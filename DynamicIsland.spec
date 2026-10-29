# PyInstaller spec: windowed one-folder build -> dist\DynamicIsland\DynamicIsland.exe
# One-folder (not one-file) so autostart at logon does not unpack ~100 MB to %TEMP% every time.
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

hiddenimports = collect_submodules("winrt") + collect_submodules("pycaw") + collect_submodules("comtypes", filter=lambda name: ".test" not in name) + ["win32timezone"]
binaries = collect_dynamic_libs("winrt")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=[("assets/icon.ico", "assets")],
    hiddenimports=hiddenimports,
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml",
              "PySide6.QtQuick", "PySide6.Qt3DCore", "PySide6.QtPdf", "PySide6.QtMultimedia",
              "pytest", "pytestqt"],
    noarchive=False,
)

# The UI is pure QWidget/QPainter: drop the software-OpenGL fallback, Qt Quick/QML and the PDF stack
# that plugins drag in (~45 MB). Plugins depending on them are dropped too so nothing fails to load.
_DROP = ("opengl32sw.dll", "qt6quick", "qt6qml", "qt6pdf", "qt6virtualkeyboard", "qpdf.dll",
         "qtvirtualkeyboardplugin.dll", "qt6opengl.dll", "qt6openglwidgets.dll")
a.binaries = [b for b in a.binaries if not any(part in b[0].lower().replace("\\", "/").rsplit("/", 1)[-1]
                                                for part in _DROP)]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DynamicIsland",
    icon="assets/icon.ico",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="DynamicIsland", upx=False)
