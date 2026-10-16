"""Persistent target-side IPython execution; independent of HTTP transport."""

import queue
import sys
import threading
import time


class Kernel:
    def __init__(self, python=None):
        from jupyter_client import KernelManager

        self.lock = threading.Lock()
        self.manager = KernelManager(kernel_name="python3")
        self.manager.kernel_spec.argv[0] = python or sys.executable
        try:
            self.manager.start_kernel()
            self.client = self.manager.client()
            self.client.start_channels()
            self.client.wait_for_ready(timeout=60)
        except BaseException:
            self.manager.shutdown_kernel(now=True)
            raise

    def close(self):
        self.client.stop_channels()
        self.manager.shutdown_kernel(now=True)

    def run(self, code, timeout):
        with self.lock:
            identity = self.client.execute(code, allow_stdin=False, stop_on_error=True)
            deadline = time.monotonic() + timeout
            content = []
            failed = False
            text_bytes = image_bytes = 0
            display_ids = {}
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Restart rather than allow a timed-out cell to continue mutating the task.
                    self.manager.interrupt_kernel()
                    self.manager.restart_kernel(now=True)
                    self.client.wait_for_ready(timeout=15)
                    content.append(
                        {
                            "type": "text",
                            "text": "Cell timed out; kernel restarted and variables were reset.",
                        }
                    )
                    return {"content": content, "isError": True, "kernel_reset": True}
                try:
                    message = self.client.get_iopub_msg(timeout=min(remaining, 1))
                except queue.Empty:
                    continue
                if message.get("parent_header", {}).get("msg_id") != identity:
                    continue
                kind, data = message["msg_type"], message["content"]
                if kind == "status" and data["execution_state"] == "idle":
                    break
                if kind == "clear_output":
                    content.clear()
                    display_ids.clear()
                    continue
                block = None
                if kind in ("display_data", "execute_result", "update_display_data"):
                    mime = data.get("data", {})
                    image_type = next(
                        (key for key in ("image/png", "image/jpeg") if key in mime), None
                    )
                    if image_type:
                        value = mime[image_type]
                        image_bytes += len(value)
                        if image_bytes > 16 * 1024 * 1024:
                            block = {"type": "text", "text": "[image output limit reached]"}
                        else:
                            block = {"type": "image", "data": value, "mimeType": image_type}
                    elif "text/plain" in mime:
                        block = {"type": "text", "text": mime["text/plain"]}
                elif kind == "stream":
                    block = {"type": "text", "text": data["text"]}
                elif kind == "error":
                    failed = True
                    block = {"type": "text", "text": "\n".join(data["traceback"])}
                if block:
                    if block["type"] == "text":
                        available = max(0, 64000 - text_bytes)
                        block["text"] = block["text"][:available]
                        text_bytes += len(block["text"])
                    display_id = data.get("transient", {}).get("display_id")
                    if kind == "update_display_data" and display_id in display_ids:
                        content[display_ids[display_id]] = block
                    else:
                        if display_id:
                            display_ids[display_id] = len(content)
                        content.append(block)
            return {
                "content": content or [{"type": "text", "text": "Cell completed."}],
                "isError": failed,
            }
