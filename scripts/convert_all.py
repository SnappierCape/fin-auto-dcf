from pathlib import Path
import subprocess

DIR = Path("/code/fin-auto-dcf/data/10k")

for i in DIR.glob("*.htm"):
    
    parts = i.stem.split("_")
    
    if len(parts) == 3:
        continue
    
    elif len(parts) == 4:
        cik, date, _, stmt = parts
        
        print("========================")
        print(f"Processing CIK: {cik} | Statement: {stmt}")
        print("========================")
        
        subprocess.run(["uv", "run", "/code/fin-auto-dcf/src/llm/convert.py", cik, stmt], check=True)
    
    else:
        continue