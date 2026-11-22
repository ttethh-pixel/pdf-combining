import io
import json
import sys
import tempfile
import threading
from pathlib import Path
from http.server import HTTPServer,BaseHTTPRequestHandler
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
                       total=dict(raw="2" if n==1 else None,state="read" if n==1 else "absent"))
        self.send_response(200);self.end_headers()
        self.wfile.write(json.dumps(dict(choices=[dict(message=dict(content=json.dumps(reply)))])).encode())

win=app.tk.Tk()
win.withdraw()
gui=app.App(win)
win.update_idletasks()
assert "Ollama"==gui.v["provider"].get()
win.destroy()
server=HTTPServer(("127.0.0.1",0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
client=app.Client(f"http://127.0.0.1:{server.server_port}/v1","test-only","fixture-vision")
client.test()
with tempfile.TemporaryDirectory() as tmp:
    src=Path(tmp)/"сканы";src.mkdir()
    out=Path(tmp)/"вывод";out.mkdir()
    for n,name in [(1,"АБ.001"),(2,"АБ.001(3)")]:
        im=Image.new("RGB",(600,400),"white")
        ImageDraw.Draw(im).text((340,310),f"AB.001    {n}",fill="black",font_size=28)
        im.save(src/(name+".pdf"))
    server.sheets=[2,1]
    a=SimpleNamespace(source=src,model="fixture-vision",tesseract=None)
    with app.session(out) as (db,job):
        app.core.scan(db,job,a,client)
        rows,p=app.publish(db,job,out,print)
        assert not list(out.glob("*.pdf"))
        rid=next(r["id"] for r in rows if r["sheet"]==2)
        db.execute("UPDATE pages SET decision='accept',edited=?,comment='проверено' WHERE id=?",
                   (json.dumps(["АБ.001",2,None]),rid));db.commit()
        app.publish(db,job,out,print)
        files=list(out.glob("*.pdf"))
        assert len(files)==1 and "СОШЛОСЬ" in (out/"Отчёт.txt").read_text(encoding="utf-8-sig")
        app.publish(db,job,out,print)
        assert list(out.glob("*.pdf"))==files
        (src/"АБ.001(3).pdf").unlink()
        app.core.scan(db,job,a,client)
        app.publish(db,job,out,print)
        assert not list(out.glob("*.pdf"))
        assert "Отсутствуют до известной границы: 2" in (out/"Отчёт.txt").read_text(encoding="utf-8-sig")
        assert list((job/"предыдущие").rglob("*.pdf"))
server.shutdown()
print("PASS portable runtime, GUI startup, vision API, conflict, publication, archive and gap report")

