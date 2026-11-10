import importlib.util
import sys
from pathlib import Path

def register(ctx):
    root = Path(__file__).resolve().parents[4] / 'shared' / 'academic-workflow'
    sys.path.insert(0,str(root))
    spec=importlib.util.spec_from_file_location("academic_workflow_plugin",root/"plugin.py")
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.register(ctx)
