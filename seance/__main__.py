"""`python3 -m seance` (service files written before 4.5) starts Noxa."""
import runpy

runpy.run_module("noxa", run_name="__main__", alter_sys=True)
