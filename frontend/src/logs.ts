export type WorkEvent = {
  seq: number;
  kind: string;
  entity_id: string;
  body: any;
  created: number;
  _bytes?: number;
};

export function extractOutput(event: WorkEvent): string {
  if (event.kind !== "codex.event") return "";
  const { method, params = {} } = event.body;
  let text = "";
  if (method?.endsWith("/delta") || method?.endsWith("/outputDelta")) {
    text = params.delta || params.outputDelta || "";
  } else if (method === "error") {
    text = `\n[错误] ${params.error?.message || JSON.stringify(params.error)}\n`;
  } else if (method === "item/completed") {
    if (params.item?.type === "commandExecution") text = params.item.aggregatedOutput || "";
    if (params.item?.type === "agentMessage") text = params.item.text || "";
  }
  return text.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "").replace(/\x00/g, "");
}

export class OutputBuffer {
  private chunks: { id: string; text: string }[] = [];
  private length = 0;

  add(event: WorkEvent) {
    const text = extractOutput(event);
    if (!text) return;
    const params = event.body.params || {};
    const id = params.itemId || params.item_id || params.item?.id || `event-${event.seq}`;
    const chunk = this.chunks.find((chunk) => chunk.id === id);
    if (chunk) {
      this.length -= chunk.text.length;
      chunk.text = event.body.method === "item/completed" ? text : chunk.text + text;
      this.length += chunk.text.length;
    } else {
      this.chunks.push({ id, text });
      this.length += text.length;
    }
    while (this.chunks.length > 512 || (this.length > 128000 && this.chunks.length > 1)) {
      this.length -= this.chunks.shift()!.text.length;
    }
    if (this.length > 128000) {
      this.chunks[0].text = this.chunks[0].text.slice(-128000);
      this.length = this.chunks[0].text.length;
    }
  }

  text() {
    return this.chunks.map((chunk) => chunk.text).join("");
  }
}
