"""Windows GUI для проверки и сборки чертежей."""
import base64
import contextlib
import io
import json
import msvcrt
import os
import queue
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib import error, parse, request
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from PIL import Image, ImageDraw, ImageTk
import eskd_qwen as core

PROVIDERS = {
    "Ollama": ("http://127.0.0.1:11434/v1", "qwen2.5vl:7b"),
    "LM Studio": ("http://127.0.0.1:1234/v1", ""),
    "LiteLLM Proxy": ("http://127.0.0.1:4000/v1", ""),
    "OpenAI API": ("https://api.openai.com/v1", ""),
    "Другой OpenAI-совместимый API": ("", ""),
}
BULK_CONFIRMATION = "Подтверждено в списке страниц"


def valid_page_fields(designation, sheet, total):
    return bool(designation and designation.strip() and sheet
                and (sheet != 1 or total) and (not total or sheet <= total))


def can_confirm(row):
    return bool(row["id"] and not row["decision"] and valid_page_fields(row["d"], row["sheet"], row["qty"]))


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Client:
    def __init__(self, url, key, model):
        url = url.strip().rstrip("/")
        u = parse.urlsplit(url)
        if u.scheme not in ("https","http") or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("Введите базовый HTTP(S)-адрес API без ключа, параметров и #.")
        if not model.strip():
            raise ValueError("Введите идентификатор визуальной модели.")
        self.url = url if url.endswith("/chat/completions") else url + "/chat/completions"
        self.key, self.model = key.strip(), model.strip()
        self.identity = "chat-v1|" + self.url + "|" + self.model
        self.http = request.build_opener(NoRedirect())

    def ask(self, model, prompt, images, schema):
        content = [dict(type="text", text=prompt+"\nВерни только JSON по схеме:\n"+json.dumps(schema,ensure_ascii=False))]
        for image in images:
            uri = "data:image/png;base64,"+base64.b64encode(image).decode("ascii")
            content.append(dict(type="image_url", image_url={"url":uri}))
        data = json.dumps(dict(model=model, messages=[dict(role="user", content=content)],stream=False)).encode()
        headers = {"Content-Type":"application/json"}
        if self.key:
            headers["Authorization"] = "Bearer "+self.key
        req = request.Request(self.url, data, headers)
        for attempt in range(3):
            try:
                with self.http.open(req,timeout=300) as res:
                    reply = json.load(res)["choices"][0]["message"]["content"]
                break
            except error.HTTPError as exc:
                if exc.code in (429,502,503,504) and attempt<2:
                    time.sleep(2**attempt)
                    continue
                # Never persist a server echo of the key or of image data.
                raise ValueError(f"API HTTP {exc.code}. Проверьте адрес, ключ, модель и поддержку изображений.") from exc
        if not isinstance(reply,str):
            raise ValueError("API не вернул текстовый JSON")
        reply = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$","",reply, flags=re.I)
        return json.loads(reply)

    def test(self):
        im = Image.new("RGB",(240,100),"white")
        ImageDraw.Draw(im).text((35,20),"74", fill="black",font_size=54)
        buf=io.BytesIO()
        im.save(buf,format="PNG")
        ans=self.ask(self.model,"Прочти число на картинке в поле value.",[buf.getvalue()],
                     core.obj(value={"type":"integer"}))
        if str(ans.get("value"))!="74":
            raise ValueError("Ответ получен, но контрольное число с картинки не распознано.")


@contextlib.contextmanager
def session(out):
    job=out/".eskd"
    job.mkdir(parents=True,exist_ok=True)
    with (job/"session.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            raise ValueError("С этой папкой уже работает другой экземпляр.")
        with contextlib.closing(sqlite3.connect(job/"реестр.sqlite")) as db:
            db.row_factory=sqlite3.Row
            core.initialize(db)
            yield db,job


def publish(db, job, out, log, *, root=None):
    if core.config(db, "complete") != "1":
        raise ValueError("Сканирование прервано. Продолжите обработку.")
    if root is None:
        root = core.verify_inventory(db)
    rows,plans=core.report(db,job)
    index=json.loads(core.config(db,"published") or "{}")
    archived=job/"предыдущие"/(time.strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:6])
    def retire(d):
        item=index.get(d)
        if not item:
            return
        path=out/item["name"]
        if path.is_file() and core.digest(path.read_bytes())==item["hash"]:
            archived.mkdir(parents=True,exist_ok=True)
            path.rename(archived/path.name)
        del index[d]
        core.config(db,"published",json.dumps(index))
        db.commit()
    lines=["Проверка и сборка чертежей ЕСКД",time.strftime("%d.%m.%Y %H:%M:%S"),
           "Исходная папка: "+str(root),"PDF в описи: "+str(db.execute("SELECT count(*) FROM files").fetchone()[0]),""]
    for d in set(index)-set(plans):
        retire(d)
    for d,p in sorted(plans.items()):
        items=p["selected"]
        nums=core.seq(r["sheet"] for r in items)
        lines.append(f'{d} | файлов-кандидатов: {len({r["path"] for r in p["rows"]})} | листов по штампу: {p["qty"] or "неизвестно"} | найдены: {nums or "—"}')
        status=""
        if p["errors"]:
            retire(d)
            status="НЕ СОБРАНО. "+"; ".join(p["errors"])
            if p["missing"]:
                status+=". Отсутствуют до известной границы: "+core.seq(p["missing"])
            if p["duplicates"]:
                status+=". Дубли листов: "+core.seq(p["duplicates"])
        else:
            signature=core.digest(json.dumps([r["id"] for r in items]).encode())
            old=index.get(d,{})
            dest=out/old["name"] if old else None
            try:
                if not (old.get("signature")==signature and dest.is_file() and core.digest(dest.read_bytes())==old["hash"]):
                    slug=re.sub(r'[^\w. -]',"_",d)[:100].rstrip(" .")
                    name="КД_"+slug+"_"+core.digest(d.encode())[:8]+".pdf"
                    dest=out/name
                    if dest.exists():
                        dest=out/(dest.stem+"_"+uuid.uuid4().hex[:8]+".pdf")
                    stage=job/"сборка"
                    stage.mkdir(exist_ok=True)
                    core.assemble(root,d,p,stage/dest.name)
                    retire(d)
                    (stage/dest.name).replace(dest)
                    index[d]=dict(name=dest.name,hash=core.digest(dest.read_bytes()),signature=signature)
                    core.config(db,"published",json.dumps(index))
                    db.commit()
                p["output"] = dest.name
                status="СОШЛОСЬ. Все листы проверены и собраны: "+dest.name
            except Exception as exc:
                status="ОШИБКА СБОРКИ: "+str(exc)
        lines.append(status)
        log(d+" — "+status)
        for r in sorted(p["rows"],key=lambda r:(r["sheet"] or 0,r["path"])):
            lines.append(f'  Лист {r["sheet"] or "?"} | {r["path"]} | стр. PDF {r["n"] or "?"} | '+
                         ("; ".join(r["reasons"]) or r["comment"] or "сверено"))
        lines.append("")
    for r in rows:
        if r["decision"]=="skip":
            lines.append(f'Исключено вручную: {r["path"]} стр. {r["n"]}. {r["comment"]}')
    if not plans:
        lines.append("Комплектов для сборки нет.")
    for name in ("Страницы.csv","Комплекты.csv"):
        temp=job/(name+".new")
        temp.write_bytes((job/name).read_bytes())
        temp.replace(out/name)
    temp=job/"report.new"
    temp.write_text("\n".join(lines)+"\n",encoding="utf-8-sig")
    temp.replace(out/"Отчёт.txt")
    return rows,plans


class App:
    def __init__(self,window):
        self.win=window
        window.title("Чертежи ЕСКД · проверка и сборка PDF")
        window.geometry("970x720")
        window.minsize(760,600)
        self.events=queue.Queue()
        self.busy=False
        self.controls=[]
        self.checked = set()
        self.confirmable = set()
        self.settings=Path(os.environ.get("LOCALAPPDATA",str(Path.home())))/"EskdDrawingReview"/"settings.json"
        try:
            saved=json.loads(self.settings.read_text(encoding="utf-8"))
        except (OSError,ValueError):
            saved={}
        self.v={k:tk.StringVar(value=saved.get(k,default)) for k,default in
                dict(source="",out="",provider="Ollama",url=PROVIDERS["Ollama"][0],model="qwen2.5vl:7b").items()}
        self.v["key"]=tk.StringVar()
        top=ttk.Frame(window,padding=12)
        top.pack(fill="both",expand=True)
        form=ttk.Frame(top)
        form.pack(fill="x")
        form.columnconfigure(1,weight=1)
        windows = window.tk.call("tk", "windowingsystem") == "win32"
        for i,(k,title) in enumerate([("source","Исходные PDF"),("out","Папка вывода"),("provider","Провайдер"),
                                      ("url","Базовый URL API"),("model","Модель"),("key","Ключ API")]):
            ttk.Label(form,text=title).grid(row=i,column=0,sticky="w",padx=(0,12),pady=3)
            if k=="provider":
                w=ttk.Combobox(form,values=list(PROVIDERS),textvariable=self.v[k],state="readonly")
                w.bind("<<ComboboxSelected>>",self.provider)
            else:
                w=ttk.Entry(form,textvariable=self.v[k],show="•" if k=="key" else "")
            w.grid(row=i,column=1,sticky="ew",pady=2)
            self.controls.append((w,"readonly" if k=="provider" else "normal"))
            if k in ("source","out"):
                b=ttk.Button(form,text="Обзор…",command=lambda k=k:self.pick(k))
                b.grid(row=i,column=2,padx=5)
                self.controls.append((b,"normal"))
            elif k in ("url", "key"):
                if windows:
                    w.bind("<Control-KeyPress>", self.paste_shortcut)
                b = ttk.Button(form, text="Вставить", command=lambda entry=w: self.paste_field(entry))
                b.grid(row=i, column=2, padx=5)
                self.controls.append((b, "normal"))
        ttk.Label(top,text="На указанный API передаются обзор страницы и фрагмент штампа. Ключ хранится в памяти до закрытия.").pack(anchor="w",pady=5)
        actions=ttk.Frame(top)
        actions.pack(fill="x",pady=6)
        for label,func in [("Проверить API",self.test),("Обработать и сшить",self.run),
                           ("Открыть реестр",self.load),("Сшить из реестра",self.build_from_registry),
                           ("Открыть результат",self.open_out)]:
            w=ttk.Button(actions,text=label,command=lambda f=func:self.safe(f))
            w.pack(side="left",padx=(0,6))
            self.controls.append((w,"normal"))
        ttk.Label(top,text="Страницы для проверки · двойной щелчок открывает карточку").pack(anchor="w",pady=(8,3))
        review_actions = ttk.Frame(top)
        review_actions.pack(fill="x")
        self.show_all=tk.BooleanVar()
        ttk.Checkbutton(review_actions,text="Показать все страницы",variable=self.show_all,command=self.display).pack(side="left")
        self.confirm_button = ttk.Button(review_actions, text="Подтвердить отмеченные",
                                         command=lambda: self.safe(self.confirm_checked), state="disabled")
        self.confirm_button.pack(side="left", padx=10)
        frame=ttk.Frame(top);frame.pack(fill="both",expand=True)
        self.tree=ttk.Treeview(frame,columns=("check","path","page","d","n","total","why"),show="headings",selectmode="browse")
        for k,title,width in [("check","✓",35),("path","Исходный файл",200),("page","Стр.",40),("d","Обозначение",145),
                              ("n","Лист",40),("total","Листов",55),("why","Состояние",270)]:
            self.tree.heading(k,text=title)
            self.tree.column(k,width=width,minwidth=30)
        self.tree.column("check", anchor="center", stretch=False)
        bar=ttk.Scrollbar(frame,command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        bar.pack(side="right",fill="y");self.tree.pack(fill="both",expand=True)
        self.tree.bind("<Button-1>", self.click_checkbox)
        self.tree.bind("<space>", self.toggle_focused)
        self.tree.bind("<Double-1>", self.open_card)
        self.status=tk.StringVar(value="Выберите папки и проверьте подключение.")
        ttk.Label(top,textvariable=self.status,wraplength=850).pack(anchor="w",pady=7)
        self.rows=[];self.plans={};self.loaded=None
        self.win.protocol("WM_DELETE_WINDOW",self.close)
        self.win.after(100,self.poll)

    def safe(self,fn):
        try:
            fn()
        except Exception as exc:
            messagebox.showerror("Чертежи ЕСКД",str(exc),parent=self.win)

    def close(self):
        if not self.busy or messagebox.askyesno("Закрыть","Прервать текущую обработку и закрыть программу? Прочитанные страницы сохранены."):
            self.win.destroy()

    def provider(self,_):
        url,model=PROVIDERS[self.v["provider"].get()]
        self.v["url"].set(url);self.v["model"].set(model);self.v["key"].set("")

    def pick(self,k):
        p=filedialog.askdirectory(title="Выберите папку")
        if p:self.v[k].set(p)

    @staticmethod
    def paste_field(entry):
        entry.focus_set()
        entry.selection_range(0, "end")
        entry.event_generate("<<Paste>>")

    @staticmethod
    def paste_shortcut(event):
        # Tk 8.6 on Windows reports a layout-dependent keysym but a Windows virtual key code.
        alt_mask = 1 << 17
        if event.keycode == ord("V") and not event.state & alt_mask:
            event.widget.event_generate("<<Paste>>")
            return "break"

    def paths(self):
        if not self.v["out"].get().strip():
            raise ValueError("Выберите отдельную папку вывода.")
        return Path(self.v["out"].get()).resolve()

    def open_out(self):
        os.startfile(self.paths())

    def client(self):
        return Client(*(self.v[k].get() for k in ("url","key","model")))

    def start(self,fn):
        if self.busy:return
        self.busy=True
        for w,_ in self.controls:w.configure(state="disabled")
        self.update_confirm_button()
        def worker():
            try: self.events.put(("done",fn()))
            except Exception as exc:self.events.put(("error",str(exc)))
        threading.Thread(target=worker,daemon=True).start()

    def poll(self):
        try:
            while True:
                kind,val=self.events.get_nowait()
                if kind=="log":
                    self.status.set(val);continue
                self.busy=False
                for w,state in self.controls:w.configure(state=state)
                self.update_confirm_button()
                if kind=="error":
                    self.status.set("Ошибка: "+val)
                    messagebox.showerror("Чертежи ЕСКД",val,parent=self.win)
                else:
                    if isinstance(val,tuple):
                        if self.loaded != val[0]:
                            self.checked.clear()
                        self.loaded,self.rows,self.plans=val
                        self.display()
                        outputs = [plan["output"] for plan in self.plans.values() if "output" in plan]
                        status = f"Готовых PDF — {len(outputs)} из {len(self.plans)}."
                        if len(outputs) == 1:
                            status += f" Сохранён {outputs[0]}."
                        self.status.set(status + " Подробности в Отчёт.txt.")
                    else:self.status.set(val)
        except queue.Empty:pass
        self.win.after(100,self.poll)

    def test(self):
        client=self.client()
        self.status.set("Проверяется чтение контрольного изображения…")
        def work():
            client.test()
            return "Подключение работает. Модель прочитала контрольное изображение."
        self.start(work)

    def run(self):
        out=self.paths()
        src=Path(self.v["source"].get()).resolve()
        if not self.v["source"].get().strip() or not src.is_dir() or out.is_relative_to(src) or src.is_relative_to(out):
            raise ValueError("Выберите две отдельные папки. Одна не должна находиться внутри другой.")
        client=self.client()
        self.settings.parent.mkdir(parents=True,exist_ok=True)
        self.settings.write_text(json.dumps({k:v.get() for k,v in self.v.items() if k!="key"},ensure_ascii=False),encoding="utf-8")
        self.status.set("Проверяется API…")
        def work():
            client.test()
            with session(out) as (db,job):
                core.scan(db,job,SimpleNamespace(source=str(src),model=client.model,tesseract=None),
                          client,self.log)
                return out,*publish(db,job,out,self.log)
        self.start(work)

    def log(self,text):
        self.events.put(("log",text))

    def registry_output(self):
        out=self.paths()
        if not (out/".eskd"/"реестр.sqlite").is_file():
            raise ValueError("В этой папке ещё нет реестра.")
        return out

    def load(self):
        out = self.registry_output()
        with session(out) as (db,_):
            if core.config(db,"complete")!="1":
                raise ValueError("Прерванное сканирование нужно продолжить кнопкой обработки.")
            self.loaded=out
            self.rows,self.plans=core.analyse(db)
        self.checked.clear()
        self.display()

    def build_from_registry(self):
        out = self.registry_output()
        self.status.set("Собираются PDF из сохранённого реестра…")

        def work():
            with session(out) as (db, job):
                return out, *publish(db, job, out, self.log)

        self.start(work)

    def display(self):
        self.tree.delete(*self.tree.get_children())
        conflicts={r["id"]: ", ".join(p["errors"]) for p in self.plans.values() if p["errors"] for r in p["rows"]}
        self.confirmable = {r["id"] for r in self.rows if r["id"] in conflicts and can_confirm(r)}
        self.checked.intersection_update(self.confirmable)
        for i,r in enumerate(self.rows):
            if not self.show_all.get() and r["id"] not in conflicts:continue
            why="; ".join(r["reasons"]) or conflicts.get(r["id"]) or r["comment"] or "Сверено"
            mark = "✓" if r["decision"] == "accept" else ""
            if r["id"] in self.confirmable:
                mark = "☑" if r["id"] in self.checked else "☐"
            self.tree.insert("","end",iid=str(i),
                             values=(mark,r["path"],r["n"],r["d"] or "—",r["sheet"] or "—",r["qty"] or "—",why))
        self.update_confirm_button()

    def click_checkbox(self, event):
        if self.tree.identify_column(event.x) == "#1":
            self.toggle_checkbox(self.tree.identify_row(event.y))

    def toggle_focused(self, _event):
        self.toggle_checkbox(self.tree.focus())
        return "break"

    def open_card(self, event):
        if self.tree.identify_column(event.x) != "#1" and self.tree.identify_row(event.y):
            self.safe(self.card)

    def toggle_checkbox(self, item):
        if self.busy or not item:
            return
        page_id = self.rows[int(item)]["id"]
        if page_id not in self.confirmable:
            return
        self.checked.symmetric_difference_update({page_id})
        self.tree.set(item, "check", "☑" if page_id in self.checked else "☐")
        self.update_confirm_button()

    def update_confirm_button(self):
        self.confirm_button.configure(state="normal" if self.checked and not self.busy else "disabled")

    def confirm_checked(self):
        if self.busy or not self.checked:
            return
        out = self.loaded
        selected = {r["id"]: r for r in self.rows if r["id"] in self.checked}
        self.status.set("Подтверждаются отмеченные страницы…")

        def work():
            with session(out) as (db, job):
                if core.config(db, "complete") != "1":
                    raise ValueError("Сканирование прервано. Повторите обработку.")
                root = core.verify_inventory(db)
                rows, _ = core.analyse(db)
                current = {r["id"]: r for r in rows if r["id"] in selected}
                stale = "Отмеченные страницы изменились. Откройте реестр заново."
                if current.keys() != selected.keys():
                    raise ValueError(stale)
                updates = []
                for page_id, previous in selected.items():
                    row = current[page_id]
                    fields = (row["d"], row["sheet"], row["qty"])
                    previous_fields = (previous["d"], previous["sheet"], previous["qty"])
                    if fields != previous_fields or row["original"] != previous["original"]:
                        raise ValueError(stale)
                    if row["decision"] == "accept" and row["comment"] == BULK_CONFIRMATION:
                        continue
                    if not can_confirm(row):
                        raise ValueError(stale)
                    updates.append((json.dumps(fields, ensure_ascii=False), BULK_CONFIRMATION, page_id))
                with db:
                    db.executemany("UPDATE pages SET decision='accept', edited=?, comment=? WHERE id=?", updates)
                return out, *publish(db, job, out, self.log, root=root)

        self.start(work)

    def card(self):
        if self.busy or not self.tree.selection():return
        r=self.rows[int(self.tree.selection()[0])]
        out=self.loaded;job=out/".eskd"
        if not r["id"]:
            raise ValueError(r["original"]+"\nИсправьте исходный PDF и повторите обработку.")
        dlg=tk.Toplevel(self.win);dlg.title(r["path"]);dlg.transient(self.win);dlg.grab_set()
        box=ttk.Frame(dlg,padding=12);box.pack(fill="both",expand=True)
        path=job/"images"/(r["id"]+".stamp.png")
        if path.exists():
            with Image.open(path) as im:
                im.thumbnail((740,230));photo=ImageTk.PhotoImage(im)
            label=ttk.Label(box,image=photo);label.image=photo;label.pack()
        ttk.Label(box,text=f'{r["path"]} · стр. PDF {r["n"]}\n'+(r["original"] or "Проверка комплекта"),wraplength=700).pack(anchor="w")
        fields=[]
        for title,value in [("Обозначение",r["d"]),("Лист",r["sheet"]),("Листов (обязательно на первом)",r["qty"]),("Основание решения",r["comment"])]:
            ttk.Label(box,text=title).pack(anchor="w")
            v=tk.StringVar(value=value or "");fields.append(v)
            ttk.Entry(box,textvariable=v,width=65).pack(fill="x")
        buttons=ttk.Frame(box);buttons.pack(pady=8)
        def decide(skip):
            d,s,q,why=(v.get().strip() for v in fields)
            n,total=core.number(s),core.number(q)
            if not why or (not skip and ((q and not total) or not valid_page_fields(d,n,total))):
                raise ValueError("Проверьте реквизиты и укажите основание решения.")
            with session(out) as (db,_):
                db.execute("UPDATE pages SET decision=?,edited=?,comment=? WHERE id=?",
                           ("skip" if skip else "accept",json.dumps([d,n,total]),why,r["id"]))
                db.commit()
            dlg.destroy()
            def work():
                with session(out) as (db,j):return out,*publish(db,j,out,self.log)
            self.start(work)
        with session(out) as (db,_):source=Path(core.config(db,"root"))/r["path"]
        for title,fn in [("Открыть PDF",lambda:os.startfile(source)),
                         ("Обзор страницы",lambda:os.startfile(job/"images"/(r["id"]+".page.png"))),
                         ("Подтвердить",lambda:decide(False)),("Исключить",lambda:decide(True))]:
            ttk.Button(buttons,text=title,command=lambda f=fn:self.safe(f)).pack(side="left",padx=3)


if __name__=="__main__":
    window=tk.Tk()
    App(window)
    window.mainloop()
