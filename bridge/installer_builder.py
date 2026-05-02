import os
import subprocess
import sys
import json

def build_single_exe(main_script: str = "main.py", config_file: str = "config.json") -> None:
    if not os.path.exists(main_script):
        raise FileNotFoundError(main_script)
    # include data files and make onefile exe
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--noconsole",
        "--add-data",
        f"{config_file}{os.pathsep}.",
        main_script,
    ]
    subprocess.run(cmd, check=True)

if __name__ == "__main__":
    build_single_exe()
