// This extension runs beside pi. Python tool handlers operate the selected Runtime.
export default async function (pi: any) {
  const url = process.env.ENVWEAVER_TOOL_URL;
  const headers = {
    Authorization: `Bearer ${process.env.ENVWEAVER_TOOL_TOKEN}`,
    "Content-Type": "application/json",
  };
  const response = await fetch(`${url}/tools`, { headers });
  if (!response.ok) throw new Error(`EnvWeaver tools: HTTP ${response.status}`);
  const tools = await response.json();
  for (const tool of tools) {
    pi.registerTool({
      ...tool,
      label: tool.name,
      async execute(_id: string, args: unknown, signal: AbortSignal) {
        const response = await fetch(`${url}/call`, {
          method: "POST", headers, signal,
          body: JSON.stringify({ name: tool.name, arguments: args }),
        });
        if (!response.ok) throw new Error(`EnvWeaver tool: HTTP ${response.status}`);
        return { details: {}, ...(await response.json()) };
      },
    });
  }
}
