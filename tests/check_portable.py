import io
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from http.server import HTTPServer,BaseHTTPRequestHandler
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"build/unpacked/app"))
import drawing_app as app
from PIL import Image, ImageDraw
from types import SimpleNamespace

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        assert self.path=="/v1/chat/completions"
        assert self.headers["Authorization"]=="Bearer test-only"
        body=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        content=body["messages"][0]["content"]
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        if "контроль" in content[0]["text"] or "Прочти число" in content[0]["text"]:
            reply={"value":74}
        elif len(content)==2:
            reply={"found":True,"clockwise":0,"box":[20,20,980,980]}
        else:
            n=self.server.sheets.pop(0)
            reply=dict(block="short" if n>1 else "full_drawing", note="",
                       designation=dict(raw="АБ.001",state="read"),sheet=dict(raw=str(n),state="read"),
                       total=dict(raw="3" if n==1 else None,state="read" if n==1 else "absent"))
        self.send_response(200);self.end_headers()
        self.wfile.write(json.dumps(dict(choices=[dict(message=dict(content=json.dumps(reply)))])).encode())

win=app.tk.Tk()
win.withdraw()
gui=app.App(win)
win.update_idletasks()
assert "Ollama"==gui.v["provider"].get()
server=HTTPServer(("127.0.0.1",0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
client=app.Client(f"http://127.0.0.1:{server.server_port}/v1","test-only","fixture-vision")
client.test()
with tempfile.TemporaryDirectory() as tmp:
    src=Path(tmp)/"сканы";src.mkdir()
    out=Path(tmp)/"вывод";out.mkdir()
    for n,name in [(1,"АБ.001"),(2,"АБ.001(4)"),(3,"АБ.001(5)")]:
        im=Image.new("RGB",(600,400),"white")
        ImageDraw.Draw(im).text((340,310),f"AB.001    {n}",fill="black",font_size=28)
        im.save(src/(name+".pdf"))
    server.sheets=[2,3,1]
    a=SimpleNamespace(source=src,model="fixture-vision",tesseract=None)
    with app.session(out) as (db,job):
        app.core.scan(db,job,a,client)
        app.publish(db,job,out,print)
        assert not list(out.glob("*.pdf"))

    gui.v["out"].set(str(out))
    gui.load()
    assert gui.confirm_button.instate(["disabled"])
    items = {gui.tree.set(item, "n"): item for item in gui.tree.get_children()}
    assert gui.tree.set(items["1"], "total") == "3"
    gui.toggle_checkbox(items["2"])
    gui.tree.focus(items["3"])
    gui.toggle_focused(None)
    assert all(gui.tree.set(items[n], "check") == "☑" for n in ("2", "3"))
    with patch.object(app.messagebox, "showerror") as show_error:
        gui.confirm_button.invoke()
        deadline = time.monotonic() + 10
        while gui.busy and time.monotonic() < deadline:
            win.update()
            time.sleep(0.01)
        assert not gui.busy and not show_error.called, show_error.call_args
    assert not gui.tree.get_children()
    assert gui.confirm_button.instate(["disabled"])
    assert {r["sheet"] for r in gui.rows if r["decision"] == "accept"} == {2, 3}
    assert all(r["comment"] == app.BULK_CONFIRMATION for r in gui.rows if r["decision"] == "accept")

    with app.session(out) as (db,job):
        files=list(out.glob("*.pdf"))
        assert len(files)==1 and "СОШЛОСЬ" in (out/"Отчёт.txt").read_text(encoding="utf-8-sig")
        app.publish(db,job,out,print)
        assert list(out.glob("*.pdf"))==files
        (src/"АБ.001(4).pdf").unlink()
        app.core.scan(db,job,a,client)
        app.publish(db,job,out,print)
        assert not list(out.glob("*.pdf"))
        assert "Отсутствуют до известной границы: 2" in (out/"Отчёт.txt").read_text(encoding="utf-8-sig")
        assert list((job/"предыдущие").rglob("*.pdf"))
    gui.load()
    items = {gui.tree.set(item, "n"): item for item in gui.tree.get_children()}
    assert gui.tree.set(items["3"], "check") == "✓"
    assert "Есть пропуски" in gui.tree.set(items["3"], "why")
win.destroy()
server.shutdown()
print("PASS portable runtime, GUI startup, vision API, bulk confirmation, publication, archive and gap report")

