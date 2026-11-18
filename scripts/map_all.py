from pathlib import Path
import subprocess

DIR = Path("/code/fin-auto-dcf/data/converted")

for i in DIR.glob("*.json"):
    if "_" in i.stem:
        cik, stmt = i.stem.split("_", 1)
        
        print("========================")
        print(f"Processing CIK: {cik} | Statement: {stmt}")
        print("========================")
        
        subprocess.run(["uv", "run", "/code/fin-auto-dcf/src/llm/map.py", cik, stmt], check=True)
        
