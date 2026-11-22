from pathlib import Path
import sys
from zipfile import ZipFile,ZIP_DEFLATED

rt=Path(sys.base_prefix)
root=Path(__file__).resolve().parents[1]
out=root/"dist"
out.mkdir(exist_ok=True)
site=Path(sys.prefix)/"Lib/site-packages"
packages=["PIL","pypdf","pypdfium2","pypdfium2_raw","pypdfium2_cfg"]
packages += [p.name for p in site.glob("*.dist-info") if p.name.split("-")[0] in {"pypdfium2","pypdf","pillow"}]
with ZipFile(out/"Чертежи_ЕСКД_Windows_x64.zip","w",ZIP_DEFLATED,compresslevel=5) as z:
    def add(path):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix!=".pyc":
            z.write(path,"runtime/"+(("Lib/site-packages/"+path.relative_to(site).as_posix()) if path.is_relative_to(site) else path.relative_to(rt).as_posix()))
    for name in ("python.exe","pythonw.exe","python3.dll","python312.dll","vcruntime140.dll","vcruntime140_1.dll","LICENSE.txt"):add(rt/name)
    for directory in [rt/"DLLs",rt/"tcl",*(site/name for name in packages)]:
        for f in directory.rglob("*"):add(f)
    for directory in (rt/"Lib").iterdir():
        if directory.name in {"site-packages","test","idlelib","ensurepip","turtledemo","__pycache__"}:continue
        if directory.is_dir():
            for f in directory.rglob("*"):add(f)
        else:add(directory)
    for f in ("drawing_app.py","eskd_qwen.py","Инструкция_для_локальной_модели.txt"):
        z.write(root/"app"/f,"app/"+f)
    z.write(root/"Как_запустить.txt","Как_запустить.txt")
    z.write(root/"build/DrawingReview.exe","Чертежи ЕСКД.exe")
    print(len(z.namelist()),"files")

