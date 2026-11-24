"""`python3 -m seance.mountd` (service files written before 4.5) starts Noxa's mount helper."""
import runpy

if __name__ == "__main__":
    runpy.run_module("noxa.mountd", run_name="__main__", alter_sys=True)
