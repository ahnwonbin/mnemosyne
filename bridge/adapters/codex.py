from bridge.adapters.base import BaseAdapter
from bridge.adapters.normalization import normalize

class CodexAdapter(BaseAdapter):
    platform_name = "codex"
    def handle_hook(self,hook_name,payload_data,cwd=None):
        return self.record_event(**normalize(self.platform_name,hook_name,payload_data,cwd))
