"""Crea (o repara) los accesos directos «Analizador de fútbol» en Escritorio y menú Inicio,
con el ícono de la app y el mismo AppUserModelID que usa app.py, para que Windows ancle
la app correctamente a la barra de tareas.  Uso: python tools/crear_accesos.py"""
import os
import shutil
import sys
from pathlib import Path

import pythoncom
from win32com.client import Dispatch
from win32com.propsys import propsys, pscon

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
APP_ID = "SteelSteel.AnalizadorFutbol"  # igual que app.APP_ID
NAME = "Analizador de fútbol"


def pythonw():
    exe = Path(sys.executable).with_name("pythonw.exe")
    return str(exe if exe.exists() else shutil.which("pythonw"))


def create(lnk):
    shell = Dispatch("WScript.Shell")
    sc = shell.CreateShortcut(str(lnk))
    sc.TargetPath = pythonw()
    sc.Arguments = f'"{ROOT / "app.py"}"'
    sc.WorkingDirectory = str(ROOT)
    sc.IconLocation = f"{ROOT / 'assets' / 'icon.ico'},0"
    sc.Description = "Analizador de fútbol — 5 grandes ligas"
    sc.Save()
    store = propsys.SHGetPropertyStoreFromParsingName(str(lnk), None, 2, propsys.IID_IPropertyStore)  # 2 = lectura/escritura
    store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID, pythoncom.VT_LPWSTR))
    store.Commit()
    print("listo:", lnk)


if __name__ == "__main__":
    desktop = Path(os.path.join(os.environ["USERPROFILE"], "Desktop"))
    programs = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    for folder in (desktop, programs):
        create(folder / f"{NAME}.lnk")
