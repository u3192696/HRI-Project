import runpy, traceback, sys
try:
    runpy.run_path("/home/danny/Projects/hri-project/cad/drawings/make-sheet.py",
                   run_name="__main__")
except SystemExit:
    raise
except Exception:
    open("/home/danny/.config/hermes/cache/scratch/tb.txt", "w").write(
        traceback.format_exc())
    print("TRACEBACK WRITTEN")
