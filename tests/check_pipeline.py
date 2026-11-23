"""Offline integration check with synthetic drawings and a local Ollama stand-in."""
import io
import json
import sys
import tempfile
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pypdfium2 as pdfium
from PIL import Image
from reportlab.pdfgen import canvas
from pypdf import PdfReader
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"app"))
import eskd_qwen as app

class Endpoint(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def reply(self, data):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
    def do_GET(self):
        self.reply({"models":[{"name":"qwen2.5vl:7b","digest":"fixture"}]})
    def do_POST(self):
        body = json.load(io.BytesIO(self.rfile.read(int(self.headers["Content-Length"]))))
        msg = body["messages"][0]
        assert ".pdf" not in msg["content"].lower()
        if len(msg["images"])==1:
            result = {"found":True,"box":[0,0,1000,1000],"clockwise":0}
        else:
            n, qty, name, block = self.server.queue.pop(0)
            def val(x):
                return {"raw":str(x) if x else None, "state":"read" if x else "absent"}
            result = dict(block=block,designation=val(name),
                          sheet=val(n),total=val(qty),note="")
            if not n:
                result["sheet"]["state"]="blank"
        self.reply({"message":{"content":json.dumps(result)}})

def drawing(path, nums):
    path.parent.mkdir(parents=True,exist_ok=True)
    c=canvas.Canvas(str(path))
    for n in nums:
        c.drawString(100,200,f"AB.001 / sheet {n}")
        c.showPage()
    c.save()

with tempfile.TemporaryDirectory() as temp:
    src=Path(temp)/"archive"; job=Path(temp)/"job"
    server=HTTPServer(("127.0.0.1",0),Endpoint)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    app.API=f"http://127.0.0.1:{server.server_port}"
    def cmd(*args,answers=()):
        with patch.object(sys,"argv",["eskd",*args,"--job",str(job)]), patch("builtins.input",side_effect=answers), redirect_stdout(io.StringIO()):
            app.main()
    drawing(src/"AB.001.pdf",[1])
    drawing(src/"AB.001(3).pdf",[2])
    Image.new("RGB",(300,200),"white").save(src/"CD.003.pdf")
    Image.new("RGB",(300,200),"white").save(src/"EF.004.pdf")
    Image.new("RGB",(300,200),"white").save(src/"ББ.002.pdf")
    server.queue=[
        (2,None,"AB.001","short"),
        (1,4,"AB.001","full_drawing"),
        (None,1,"CD.003","specification"),
        (None,2,"EF.004","specification"),
        (None,1,"ББ.002","full_drawing"),
    ]
    cmd("scan","--source",str(src))
    import csv
    def read(name):
        with (job/(name+".csv")).open(encoding="utf-8-sig") as f:
            return list(csv.DictReader(f,delimiter=";"))
    pages=read("Страницы")
    pid=next(r["ID"] for r in pages if "(3)" in r["Файл"])
    spec=next(r for r in pages if r["Из имени"]=="CD.003")
    assert spec["Графа 7"]=="" and spec["Принятый лист"]=="1"
    assert spec["Замечания распознавания"]==""
    ambiguous_spec=next(r for r in pages if r["Из имени"]=="EF.004")
    assert ambiguous_spec["Принятый лист"]=="" and ambiguous_spec["Замечания распознавания"]
    assert read("Комплекты")[0]["Пропуски до известной границы"]=="3, 4"
    cmd("merge","--yes")
    assembled=list(job.rglob("*.pdf"))
    assert len(assembled)==2
    assert any("ББ.002" in path.name for path in assembled)
    assert len(PdfReader(next(path for path in assembled if "CD.003" in path.name)).pages)==1
    cmd("review","--id",pid,answers=["д","","","","Проверено по штампу"])
    drawing(src/"more"/"AB.001.pdf",[3,4])
    server.queue=[(3,None,"AB.001","short"),(4,None,"AB.001","short")]
    cmd("scan","--source",str(src))
    assert not server.queue
    cmd("scan","--source",str(src))
    cmd("merge","--only","AB.001","--yes")
    pdf = next(job.rglob("*AB.001*.pdf"))
    assert [p.extract_text().strip() for p in PdfReader(pdf).pages]==[f"AB.001 / sheet {i}" for i in range(1,5)]
    drawing(src/"more"/"AB.001.pdf",[4])
    try:
        cmd("merge","--yes")
    except ValueError as exc:
        assert "изменился" in str(exc)
    else:
        raise AssertionError("stale source accepted")
    server.shutdown()
print("PASS: scanned / vector, missing, suffix mismatch, blank sheet in drawings and specifications, review, resume, multipage, order, stale hash")

