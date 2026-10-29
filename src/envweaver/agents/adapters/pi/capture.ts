/** Observe Pi without changing prompts, tools, or provider payloads. */
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
export default function (pi: any) {
  const path = process.env.ENVWEAVER_PI_TRACE;
  if (!path) return;
  mkdirSync(dirname(path), { recursive: true });
  let call = 0;
  let transportCall = 0;
  let phase = "agent";
  const record = (type: string, data: any, id = String(call)) => appendFileSync(path, JSON.stringify({
    type, call_id: id, time: new Date().toISOString(), data,
  }) + "\n");
  // Pi's compactor bypasses the ordinary provider-request hook. Observe fetch's JSON
  // body too; do not copy transport headers or alter the request/returned response.
  const fetch = globalThis.fetch;
  globalThis.fetch = async (input: any, init?: any) => {
    let id: string | undefined;
    try {
      const body = init?.body ?? (input instanceof Request ? await input.clone().text() : undefined);
      const payload = typeof body === "string" ? JSON.parse(body) : undefined;
      if (payload && (payload.messages || payload.input || payload.contents)) {
        id = `http:${++transportCall}`;
        record("http_request", { payload, phase, agent_call_id: String(call) }, id);
      }
    } catch (error) {
      record("capture_error", { phase, message: String(error) });
    }
    try {
      const response = await fetch(input, init);
      if (id) {
        // clone() preserves the stream consumed by the SDK. Reading the clone only
        // records native bytes; its asynchronous failure cannot fail model execution.
        const requestId = id;
        void response.clone().text().then(
          body => record("http_response", { status: response.status, body }, requestId),
          error => record("capture_error", { message: String(error) }, requestId),
        );
      }
      return response;
    } catch (error) {
      if (id) record("http_error", { message: String(error) }, id);
      throw error;
    }
  };
  pi.on("before_provider_request", (event: any) => {
    call++;
    phase = "agent";
    record("request", { payload: event.payload });
  });
  pi.on("provider_stream_event", (event: any) => record("stream", event));
  pi.on("message_end", (event: any) => record("message", event.message));
  pi.on("tool_execution_end", (event: any) => record("tool_result", event));
  pi.on("session_before_compact", (event: any) => {
    phase = "compaction";
    record("compaction_start", event);
  });
  pi.on("session_compact", (event: any) => {
    record("compaction", event);
    phase = "agent";
  });
}
