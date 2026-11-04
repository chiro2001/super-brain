import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import { extractOutput, OutputBuffer, type WorkEvent } from "./logs";

type Item = { id: string; [key: string]: any };
type Snapshot = {
  updated_at: number;
  cursor: number;
  tasks: Item[];
  sessions: Item[];
  hosts: Item[];
  backends: Item[];
  routes: Item[];
  recoveries: Item[];
  proposals: Item[];
  decisions: Item[];
  supervisor: { configured: boolean };
};
type Event = WorkEvent;
const blank: Snapshot = {
  updated_at: 0,
  cursor: 0,
  tasks: [],
  sessions: [],
  hosts: [],
  backends: [],
  routes: [],
  recoveries: [],
  proposals: [],
  decisions: [],
  supervisor: { configured: false },
};
const labels: Record<string, string> = {
  planned: "待安排",
  ready: "就绪",
  running: "执行中",
  active: "执行中",
  inProgress: "执行中",
  idle: "空闲",
  complete: "已完成",
  completed: "已完成",
  paused: "已暂停",
  blocked: "已阻塞",
  failed: "失败",
  waiting_resource: "等待资源",
  unknown: "未知",
  unavailable: "不可用",
  available: "可用",
  connected: "已连接",
  disconnected: "未连接",
  connecting: "连接中",
  error: "连接异常",
  unloaded: "未加载",
  notLoaded: "未加载",
  systemError: "会话错误",
  observing: "监测中",
  suspected_stall: "疑似停滞",
  confirmed_stall: "确认停滞",
  observation_lost: "观测断开",
  waiting: "等待中",
  tool_running: "工具执行中",
  cooldown: "恢复冷却",
  pending: "待决定",
  awaiting_progress: "等待恢复推进",
  compressing: "恢复压缩中",
  native_compacting: "原生压缩中",
  continuing: "正在接续",
  reconcile_required: "需要核对状态",
  selecting_target: "选择执行入口",
  selecting_compression: "切换压缩入口",
  preparing: "准备恢复",
  budgetLimited: "预算达到上限",
  usageLimited: "用量受限",
  waiting_children: "等待子代理",
  slow_streaming: "持续输出中",
};
const label = (s?: string) => labels[s || "unknown"] || s || "未知";
const duration = (s?: number) =>
  s == null
    ? "—"
    : s < 60
      ? `${Math.floor(s)} 秒`
      : s < 3600
        ? `${Math.floor(s / 60)} 分 ${Math.floor(s % 60)} 秒`
        : `${Math.floor(s / 3600)} 时 ${Math.floor((s % 3600) / 60)} 分`;
const relative = (t?: number) =>
  t ? `${duration(Math.max(0, Date.now() / 1000 - t))}前` : "尚无记录";
const clock = (t?: number) =>
  t ? new Date(t * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : "—";
const operationId = () =>
  globalThis.crypto?.randomUUID?.() ||
  `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;

async function api(path: string, data?: any, method?: string) {
  const response = await fetch(path, {
    method: method || (data === undefined ? "GET" : "POST"),
    headers: data === undefined ? {} : { "Content-Type": "application/json" },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  if (!response.ok) {
    const body = await response
      .json()
      .catch(() => ({ detail: response.statusText }));
    throw new Error(
      response.status === 401
        ? "需要登录"
        : typeof body.detail === "string"
          ? body.detail
          : JSON.stringify(body.detail),
    );
  }
  return response.json();
}
function output(event: Event): string {
  return extractOutput(event);
}
function Icon({ name }: { name: string }) {
  const paths: Record<string, string> = {
    grid: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
    task: "M9 5h12 M9 12h12 M9 19h12 M3 5l1 1 2-2 M3 12l1 1 2-2 M3 19l1 1 2-2",
    server: "M3 3h18v7H3z M3 14h18v7H3z M6 6h1 M6 17h1",
    plus: "M12 5v14 M5 12h14",
    arrow: "M5 12h14 M13 6l6 6-6 6",
    brain:
      "M9 3a4 4 0 0 0-4 4 4 4 0 0 0-2 7 4 4 0 0 0 6 6V3 M15 3a4 4 0 0 1 4 4 4 4 0 0 1 2 7 4 4 0 0 1-6 6V3",
    search: "M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
    close: "M6 6l12 12 M18 6L6 18",
    pulse: "M2 12h5l3-8 4 16 3-8h5",
  };
  return (
    <svg
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={paths[name] || paths.grid} />
    </svg>
  );
}
function Badge({
  state,
  children,
}: {
  state?: string;
  children?: React.ReactNode;
}) {
  return (
    <span className={`badge ${state || ""}`}>
      <i />
      {children || label(state)}
    </span>
  );
}

function App() {
  const [focus, setFocus] = useState(false);
  const [state, setState] = useState<Snapshot>(blank);
  const [view, setView] = useState("sessions");
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState("");
  const [login, setLogin] = useState(false);
  const [token, setToken] = useState("");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [logs, setLogs] = useState<Record<string, string>>({});
  const [paused, setPaused] = useState<Set<string>>(new Set());
  const [modal, setModal] = useState<{ kind: string; item?: Item } | null>(
    null,
  );
  const [history, setHistory] = useState<Event[]>([]);
  const [report, setReport] = useState("");
  const [busy, setBusy] = useState(false);
  const [, tick] = useState(0);
  const pending = useRef<Event[]>([]);
  const pendingBytes = useRef(0);
  const buffers = useRef<Record<string, OutputBuffer>>({});
  const logCursors = useRef<Record<string, number>>({});
  const cursor = useRef(0);
  const logsRef = useRef<Record<string, string>>({});
  const pauseRef = useRef(paused);
  pauseRef.current = paused;

  async function refresh() {
    try {
      const data: Snapshot = await api("/api/snapshot");
      setState(data);
      cursor.current = Math.max(cursor.current, data.cursor);
      setLogin(false);
      return data;
    } catch (e) {
      const text = String(e);
      if (text.includes("需要登录")) setLogin(true);
      else setError(text);
      throw e;
    }
  }
  useEffect(() => {
    let source: EventSource | undefined,
      disposed = false;
    async function connect() {
      try {
        const data = await refresh();
        await Promise.all(
          data.sessions.map(async (session) => {
            const events: Event[] = await api(
              `/api/sessions/${encodeURIComponent(session.id)}/recent?limit=150`,
            );
            const buffer = new OutputBuffer();
            events.forEach((event) => buffer.add(event));
            buffers.current[session.id] = buffer;
            logsRef.current[session.id] = buffer.text();
            logCursors.current[session.id] = events.at(-1)?.seq || 0;
          }),
        );
        setLogs({ ...logsRef.current });
        if (disposed) return;
        source = new EventSource(`/api/stream?after=${data.cursor}`);
        source.onopen = () => setConnected(true);
        source.onerror = () => setConnected(false);
        source.addEventListener("update", (message) => {
          const text = (message as MessageEvent).data as string;
          const event = JSON.parse(text) as Event;
          event._bytes = text.length * 2;
          pending.current.push(event);
          pendingBytes.current += event._bytes;
          let dropped = false;
          while (
            pending.current.length > 4096 ||
            pendingBytes.current > 8_000_000
          ) {
            pendingBytes.current -= pending.current.shift()?._bytes || 0;
            dropped = true;
          }
          if (dropped)
            setError(
              "输出过快，页面缓存已截断；完整记录仍可在历史中分页查看。",
            );
        });
      } catch {
        /* 登录后重新建立连接 */
      }
    }
    connect();
    const timer = setInterval(() => {
      const batch = pending.current.splice(0);
      pendingBytes.current = 0;
      if (!batch.length) return;
      setState((previous) => {
        const next = { ...previous };
        for (const event of batch) {
          cursor.current = Math.max(cursor.current, event.seq);
          if (event.kind.endsWith(".updated")) {
            const key = (
              {
                task: "tasks",
                session: "sessions",
                host: "hosts",
                backend: "backends",
                route: "routes",
                recovery: "recoveries",
                proposal: "proposals",
                decision: "decisions",
              } as Record<string, keyof Snapshot>
            )[event.kind.split(".")[0]];
            if (key) {
              const list = next[key] as Item[];
              const old = list.find((i) => i.id === event.body.id);
              (next as any)[key] = old
                ? list.map((i) =>
                    i.id === event.body.id &&
                    (event.body.revision || 0) >= (i.revision || 0)
                      ? { ...i, ...event.body }
                      : i,
                  )
                : [...list, event.body];
            }
          }
          if (
            output(event) &&
            event.seq > (logCursors.current[event.entity_id] || 0)
          ) {
            const buffer = (buffers.current[event.entity_id] ||=
              new OutputBuffer());
            buffer.add(event);
            logsRef.current[event.entity_id] = buffer.text();
            logCursors.current[event.entity_id] = event.seq;
          }
        }
        next.cursor = cursor.current;
        next.updated_at = Date.now() / 1000;
        return next;
      });
      setLogs((previous) => {
        const next = { ...previous };
        for (const [id, text] of Object.entries(logsRef.current))
          if (!pauseRef.current.has(id)) next[id] = text;
        return next;
      });
    }, 150);
    const reconcile = setInterval(() => {
      refresh().catch(() => {});
    }, 15000);
    return () => {
      disposed = true;
      source?.close();
      clearInterval(timer);
      clearInterval(reconcile);
    };
  }, [login]);
  useEffect(() => {
    const timer = setInterval(() => tick((v) => v + 1), 1000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    if (!modal?.item || !["task", "session"].includes(modal.kind)) return;
    const list = modal.kind === "task" ? state.tasks : state.sessions;
    const current = list.find((item) => item.id === modal.item?.id);
    if (current && current.revision !== modal.item.revision)
      setModal({ ...modal, item: current });
  }, [state]);

  async function action(fn: () => Promise<any>) {
    setBusy(true);
    setError("");
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  function open(item: Item, kind: string) {
    setModal({ kind, item });
    setHistory([]);
  }
  async function loadHistory(id: string) {
    const after = history.at(-1)?.seq || 0;
    const rows: Event[] = await api(
      `/api/events?entity_id=${encodeURIComponent(id)}&after=${after}&limit=100`,
    );
    setHistory((previous) => [...previous, ...rows].slice(-1000));
  }
  const active = state.sessions.filter((s) => s.status === "active").length;
  const blocked = state.tasks.filter((t) => t.status === "blocked").length;
  const approvals = state.proposals.filter((p) => p.state === "pending");
  const taskChildren = new Map<string, { total: number; complete: number }>();
  for (const task of state.tasks) {
    if (!task.parent_id) continue;
    const counts = taskChildren.get(task.parent_id) || {
      total: 0,
      complete: 0,
    };
    counts.total += 1;
    if (task.status === "complete" && task.verification?.passed)
      counts.complete += 1;
    taskChildren.set(task.parent_id, counts);
  }
  const visible = state.sessions.filter(
    (s) =>
      (filter === "all" ||
        (filter === "active"
          ? s.status === "active"
          : !s.connected ||
            s.last_error ||
            s.stall?.state?.includes("stall"))) &&
      `${s.name} ${s.cwd} ${s.model} ${s.host_id} ${state.tasks.find((t) => t.id === s.task_id)?.title || ""}`
        .toLowerCase()
        .includes(query.toLowerCase()),
  );

  return (
    <div className={`app ${focus ? "focus" : ""}`}>
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">
            <Icon name="brain" />
          </div>
          <div>
            SUPER BRAIN<small>工作任务控制台</small>
          </div>
        </div>
        <div className="section-label">工作空间</div>
        {[
          ["sessions", "grid", "会话监控", state.sessions.length],
          ["tasks", "task", "工作任务", state.tasks.length],
          ["resources", "server", "算力与主机", state.hosts.length],
        ].map(([id, icon, title, count]) => (
          <button
            key={id}
            className={`nav ${view === id ? "selected" : ""}`}
            onClick={() => setView(String(id))}
          >
            <Icon name={String(icon)} />
            <span>{title}</span>
            <em>{count}</em>
          </button>
        ))}
        <div className="sidebar-rule" />
        <div className="section-label">
          需要关注 <span>{approvals.length + blocked}</span>
        </div>
        {approvals.slice(0, 5).map((p) => (
          <button
            className="attention"
            key={p.id}
            onClick={() => open(p, "proposal")}
          >
            <span className="dot amber" />
            <div>
              范围变更待决定
              <small>
                {state.tasks.find((t) => t.id === p.task_id)?.title ||
                  p.task_id}
              </small>
            </div>
            <Icon name="arrow" />
          </button>
        ))}
        {blocked ? (
          <div className="subtle sidebar-copy">
            {blocked} 项任务需要继续处理
          </div>
        ) : (
          !approvals.length && (
            <div className="quiet">
              <span>✓</span> 暂无待决定事项
            </div>
          )
        )}
        <div className="sidebar-footer">
          <div className={`connection ${connected ? "online" : ""}`}>
            <i />
            {connected ? "实时连接正常" : "等待连接"}
            <span>{clock(state.updated_at)}</span>
          </div>
          <small>
            {state.supervisor.configured
              ? "管理 agent 已配置"
              : "管理 agent 待接入"}
          </small>
        </div>
      </aside>
      <main>
        <header>
          <div className="breadcrumb">
            工作空间 <span>/</span>{" "}
            {view === "sessions"
              ? "会话监控"
              : view === "tasks"
                ? "工作任务"
                : "算力与主机"}
          </div>
          <div className="header-actions">
            <span className={`live ${connected ? "" : "offline"}`}>
              <i />
              {connected ? "实时" : "离线"}
            </span>
            {view === "sessions" && (
              <button onClick={() => setFocus(!focus)}>
                {focus ? "退出专注" : "专注监控"}
              </button>
            )}
            <button
              onClick={() =>
                action(async () => {
                  setReport((await api("/api/report")).text);
                  setModal({ kind: "report" });
                })
              }
            >
              文字快照
            </button>
            <button
              className="primary"
              onClick={() =>
                setModal({
                  kind: view === "resources" ? "host-create" : "task-create",
                })
              }
            >
              <Icon name="plus" />
              {view === "resources" ? "添加主机" : "录入任务"}
            </button>
          </div>
        </header>
        <section className="page-heading">
          <div>
            <div className="eyebrow">让工作始终在视野中</div>
            <h1>
              {view === "sessions"
                ? "所有工作，一眼掌握。"
                : view === "tasks"
                  ? "从目标到交付。"
                  : "看见任务背后的算力。"}
            </h1>
            <p>
              {view === "sessions"
                ? "持续关注进展，把注意力留给真正需要你决定的事。"
                : view === "tasks"
                  ? "保留每个任务的上下文、执行记录和下一步。"
                  : "按实际后端记录共享关系，未知容量保持未知。"}
            </p>
          </div>
          <div className="heading-date">
            {new Date().toLocaleDateString("zh-CN", {
              month: "long",
              day: "numeric",
              weekday: "long",
            })}
            <small>
              本地时间{" "}
              {new Date().toLocaleTimeString("zh-CN", { hour12: false })}
            </small>
          </div>
        </section>
        {error && (
          <div role="alert" className="error-banner">
            {error}
            <button onClick={() => setError("")}>关闭</button>
          </div>
        )}
        <section className="stats">
          {[
            ["活动会话", active, "green"],
            [
              "工作任务",
              state.tasks.filter((t) => t.status !== "complete").length,
              "purple",
            ],
            [
              "已完成",
              state.tasks.filter((t) => t.status === "complete").length,
              "blue",
            ],
            ["需要关注", blocked + approvals.length, "amber"],
          ].map(([title, value, color]) => (
            <div className="stat" key={title}>
              <span>{title}</span>
              <strong>{value}</strong>
              <div className={`stat-line ${color}`} />
            </div>
          ))}
        </section>
        {view === "sessions" && (
          <>
            <div className="toolbar">
              <div className="tabs">
                {[
                  ["all", "全部会话"],
                  ["active", "正在执行"],
                  ["attention", "需要关注"],
                ].map(([id, title]) => (
                  <button
                    key={id}
                    onClick={() => setFilter(id)}
                    className={filter === id ? "active" : ""}
                  >
                    {title}
                  </button>
                ))}
              </div>
              <label className="search">
                <Icon name="search" />
                <input
                  placeholder="搜索任务、主机或目录"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                />
                <kbd>⌕</kbd>
              </label>
            </div>
            <div className="session-grid">
              {visible.map((session) => {
                const task = state.tasks.find((t) => t.id === session.task_id);
                const recovery = state.recoveries.find(
                  (r) =>
                    r.session_id === session.id &&
                    !["complete", "blocked", "cancelled"].includes(r.state),
                );
                const status = !session.connected
                  ? "observation_lost"
                  : recovery?.state ||
                    (session.compaction_turn_id && session.compaction_turn_id === session.active_turn_id && "native_compacting") ||
                    (session.stall?.state === "waiting_children" &&
                      "waiting_children") ||
                    (session.waiting_reason && "waiting") ||
                    (session.stall?.state?.includes("stall") &&
                      session.stall.state) ||
                    session.status;
                return (
                  <article className="session-card" key={session.id}>
                    <div className="card-top">
                      <span className="host-icon">
                        <Icon name="server" />
                      </span>
                      <div className="card-title">
                        <strong>
                          {task?.title || session.name || "未归属会话"}
                        </strong>
                        <small>
                          {session.parent_thread_id
                            ? `子代理 ${session.agent_nickname || ""} · `
                            : ""}
                          {session.host_id} <span>·</span>{" "}
                          {session.thread_id.slice(0, 8)}
                        </small>
                      </div>
                      <Badge state={status} />
                    </div>
                    <div className="directory" title={session.cwd}>
                      ⌁ {session.cwd || "目录未知"}
                    </div>
                    <p className="section-label">
                      {session.budget_accounting === "gateway"
                        ? session.budget_ready
                          ? "请求预算已接入"
                          : "预算入口待核对，暂不下发工作"
                        : "原生用量估算"}
                    </p>
                    <div className="goal">
                      <span className="section-label">当前目标</span>
                      <p>
                        {session.goal?.objective ||
                          task?.objective ||
                          "尚未设置目标或关联任务"}
                      </p>
                      <div>
                        <span>
                          {session.goal ? "goal 累计" : "任务执行窗口"}{" "}
                          <b>
                            {duration(
                              session.goal?.timeUsedSeconds ??
                                (task?.started_at
                                  ? Math.max(
                                      0,
                                      (task.status === "complete"
                                        ? task.verification?.at ||
                                          Date.now() / 1000
                                        : Date.now() / 1000) - task.started_at,
                                    )
                                  : undefined),
                            )}
                          </b>
                        </span>
                        <span>
                          {session.goal?.tokensUsed != null
                            ? `${Number(session.goal.tokensUsed).toLocaleString()} tokens`
                            : "用量未上报"}
                        </span>
                      </div>
                    </div>
                    <div className="terminal-top">
                      <span>
                        <i
                          className={`dot ${session.connected ? "green" : ""}`}
                        />
                        实时输出
                      </span>
                      <button
                        onClick={() =>
                          setPaused((previous) => {
                            const next = new Set(previous);
                            if (next.has(session.id)) {
                              next.delete(session.id);
                              setLogs((logs) => ({
                                ...logs,
                                [session.id]: logsRef.current[session.id] || "",
                              }));
                            } else next.add(session.id);
                            return next;
                          })
                        }
                      >
                        {paused.has(session.id) ? "继续跟随" : "暂停跟随"}
                      </button>
                    </div>
                    <Terminal
                      text={logs[session.id] || ""}
                      paused={paused.has(session.id)}
                    />
                    <div className="card-bottom">
                      <span className="model-tag">
                        {session.model || "模型未上报"}
                      </span>
                      <span title={session.progress_revision > 0 ? "最近有证据的有效推进" : "开始观察的时间，尚未记录新的有效推进"}>
                        {session.progress_revision > 0 ? "进展" : "观察"} {relative(session.last_progress_at)}
                      </span>
                      <button
                        title="打开会话详情"
                        aria-label="打开会话详情"
                        onClick={() => open(session, "session")}
                      >
                        <Icon name="arrow" />
                      </button>
                    </div>
                  </article>
                );
              })}
            </div>
            {!visible.length && (
              <div className="empty">
                <div className="empty-icon">
                  <Icon name="grid" />
                </div>
                <h2>
                  {state.sessions.length
                    ? "没有符合条件的会话"
                    : "连接你的第一个 Codex"}
                </h2>
                <p>
                  {state.sessions.length
                    ? "试试调整筛选条件。"
                    : "添加 SSH 主机及 daemon 控制端点后，已有会话会出现在这里。"}
                </p>
                <button onClick={() => setModal({ kind: "host-create" })}>
                  <Icon name="plus" />
                  添加主机
                </button>
              </div>
            )}
          </>
        )}
        {view === "tasks" && (
          <div className="task-board">
            {[
              ["planned", "待安排"],
              ["running", "执行中"],
              ["blocked", "需处理"],
              ["complete", "已交付"],
            ].map(([status, title]) => {
              const tasks = state.tasks.filter((t) =>
                status === "planned"
                  ? ["planned", "ready", "waiting_resource"].includes(t.status)
                  : status === "running"
                    ? ["running", "paused"].includes(t.status)
                    : status === "blocked"
                      ? ["blocked", "failed"].includes(t.status)
                      : t.status === status,
              );
              return (
                <section className="task-column" key={status}>
                  <h3>
                    <i
                      className={`dot ${status === "running" ? "green" : status === "blocked" ? "amber" : "purple"}`}
                    />
                    {title}
                    <span>{tasks.length}</span>
                  </h3>
                  {tasks.map((task) => (
                    <button
                      className="task-card"
                      key={task.id}
                      onClick={() => open(task, "task")}
                    >
                      <small>
                        {task.parent_id ? "子任务" : "工作任务"} · 优先级{" "}
                        {task.priority}
                      </small>
                      <h3>{task.title}</h3>
                      <p>{task.objective || "补充目标，让接续更清晰。"}</p>
                      <code>{task.cwd || "尚未分配目录"}</code>
                      <footer>
                        <Badge state={task.status} />
                        {taskChildren.has(task.id) && (
                          <span>
                            子任务 {taskChildren.get(task.id)!.complete}/
                            {taskChildren.get(task.id)!.total}
                          </span>
                        )}
                        <span>{task.dependencies.length} 项依赖</span>
                      </footer>
                    </button>
                  ))}
                  {!tasks.length && <p className="column-empty">暂无任务</p>}
                </section>
              );
            })}
          </div>
        )}
        {state.decisions.some((item) =>
          ["pending", "blocked", "waiting_user"].includes(item.state),
        ) && (
          <details className="panel attention-panel">
            <summary>
              待处理与自动处理记录 ·{" "}
              {
                state.decisions.filter((item) =>
                  ["pending", "blocked", "waiting_user"].includes(item.state),
                ).length
              }{" "}
              项
            </summary>
            {!state.supervisor.configured && (
              <p>尚未配置自动决策器，监控继续运行，以下事件等待处理。</p>
            )}
            {state.decisions
              .filter((item) =>
                ["pending", "blocked", "waiting_user"].includes(item.state),
              )
              .map((item) => (
                <div className="host-row" key={item.id}>
                  <div>
                    <strong>
                      {state.tasks.find((task) => task.id === item.task_id)
                        ?.title || item.task_id}{" "}
                      · {label(item.state)}
                    </strong>
                    <p>{item.error || item.reason}</p>
                  </div>
                </div>
              ))}
          </details>
        )}
        {view === "resources" && (
          <div className="resources">
            <section className="panel">
              <div className="panel-heading">
                <h2>执行主机</h2>
                <span>{state.hosts.length} 台已配置</span>
              </div>
              {state.hosts.map((host) => (
                <div className="host-row" key={host.id}>
                  <Icon name="server" />
                  <div>
                    <strong>{host.name}</strong>
                    <small>
                      {host.address} · {host.architecture || "架构待核查"}
                    </small>
                  </div>
                  <Badge state={host.connection} />
                  {host.last_error && (
                    <p className="subtle error-text">{host.last_error}</p>
                  )}
                </div>
              ))}
              {!state.hosts.length && (
                <p className="panel-empty">添加主机后开始监听。</p>
              )}
            </section>
            <section className="panel">
              <div className="panel-heading">
                <h2>模型入口与共享后端</h2>
                <span>按真实资源记账</span>
              </div>
              {state.routes.map((route) => (
                <div className="route-row" key={route.id}>
                  <div>
                    <strong>{route.name}</strong>
                    <small>
                      {route.purpose === "recovery"
                        ? "停滞恢复压缩入口"
                        : "常规执行入口"}
                      {!route.enabled ? " · 已禁用" : ""}
                    </small>
                  </div>
                  <span className="route-arrow">→</span>
                  <div>
                    {route.opaque ? (
                      <Badge state="unknown">后端不可见</Badge>
                    ) : (
                      route.backend_ids.map((id: string) => {
                        const backend = state.backends.find((b) => b.id === id);
                        return (
                          <span className="backend-chip" key={id}>
                            <Badge state={backend?.health} />
                            <strong>{backend?.name || id}</strong>
                            <small>
                              排队 {backend?.metrics?.waiting ?? "—"} · KV{" "}
                              {backend?.metrics?.kv_cache_usage != null
                                ? `${(backend.metrics.kv_cache_usage * 100).toFixed(0)}%`
                                : "—"}
                            </small>
                          </span>
                        );
                      })
                    )}
                  </div>
                </div>
              ))}
              {!state.routes.length && (
                <p className="panel-empty">
                  在配置文件中添加模型路由和指标地址。
                </p>
              )}
              <p className="resource-note">
                不同入口可能共享后端。不可见的上游不计作确定新增算力。
              </p>
            </section>
          </div>
        )}
        <footer className="page-footer">
          <span>
            <Icon name="pulse" />
            状态持续更新 · 正常推进的会话保持当前后端
          </span>
          <span>SUPER BRAIN / 0.1</span>
        </footer>
      </main>
      {login && (
        <div className="overlay">
          <form
            className="modal small"
            onSubmit={(e) => {
              e.preventDefault();
              action(async () => {
                await api("/api/login", { token });
                setLogin(false);
              });
            }}
          >
            <div className="eyebrow">SUPER BRAIN</div>
            <h2>连接工作空间</h2>
            <p>输入管理服务的访问令牌。</p>
            <label>
              访问令牌
              <input
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                autoFocus
              />
            </label>
            <button className="primary" disabled={busy}>
              连接
            </button>
          </form>
        </div>
      )}
      {modal && !login && (
        <div className="overlay" onClick={() => setModal(null)}>
          <section className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>
                {
                  {
                    "task-create": "录入工作任务",
                    "host-create": "添加执行主机",
                    task: "任务详情",
                    session: "会话详情",
                    proposal: "需要你决定",
                    report: "当前工作快照",
                  }[modal.kind]
                }
              </h2>
              <button aria-label="关闭" onClick={() => setModal(null)}>
                <Icon name="close" />
              </button>
            </div>
            {modal.kind === "task-create" && (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  const data = new FormData(e.currentTarget);
                  action(async () => {
                    await api("/api/tasks", {
                      title: data.get("title"),
                      objective: data.get("objective"),
                      host_id: data.get("host_id") || null,
                      cwd: data.get("cwd") || null,
                      allowed_roots: String(data.get("roots") || "")
                        .split("\n")
                        .filter(Boolean),
                      allowed_models: String(data.get("models") || "")
                        .split(",")
                        .map((v) => v.trim())
                        .filter(Boolean),
                      acceptance: data.get("acceptance"),
                      parent_id: data.get("parent_id") || null,
                      dependencies: data.getAll("dependencies").filter(Boolean),
                      priority: Number(data.get("priority") || 0),
                      budget: {
                        max_requests: Number(data.get("requests")),
                        max_tokens: Number(data.get("tokens") || 500000),
                        max_agents: Number(data.get("agents") || 0),
                        max_retries: Number(data.get("retries") || 2),
                      },
                    });
                    setModal(null);
                  });
                }}
              >
                <label>
                  任务名称
                  <input
                    name="title"
                    required
                    placeholder="例如：A1 · 实现用户接口"
                  />
                </label>
                <label>
                  工作目标
                  <textarea name="objective" rows={3} />
                </label>
                <div className="form-row">
                  <label>
                    执行主机
                    <select name="host_id">
                      <option value="">稍后分配</option>
                      {state.hosts.map((h) => (
                        <option key={h.id} value={h.id}>
                          {h.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    请求预算上限
                    <input
                      name="requests"
                      type="number"
                      min="0"
                      defaultValue="20"
                      required
                    />
                  </label>
                </div>
                <label>
                  工作目录
                  <input name="cwd" placeholder="/home/user/projects/example" />
                </label>
                <label>
                  允许操作的根目录（每行一个）
                  <textarea name="roots" rows={2} />
                </label>
                <label>
                  允许模型（逗号分隔）
                  <input name="models" placeholder="A2-dsv41f,A3-dsv41f" />
                </label>
                <label>
                  验收要求
                  <textarea name="acceptance" rows={2} />
                </label>
                <details>
                  <summary>子任务、依赖与算力预算</summary>
                  <div className="form-row">
                    <label>
                      所属工作任务
                      <select name="parent_id">
                        <option value="">独立任务</option>
                        {state.tasks.map((task) => (
                          <option key={task.id} value={task.id}>
                            {task.title}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      优先级
                      <input
                        name="priority"
                        type="number"
                        min="-100"
                        max="100"
                        defaultValue="0"
                      />
                    </label>
                  </div>
                  <label>
                    前置任务（可多选）
                    <select name="dependencies" multiple>
                      {state.tasks.map((task) => (
                        <option key={task.id} value={task.id}>
                          {task.title}
                        </option>
                      ))}
                    </select>
                  </label>
                  <div className="form-row">
                    <label>
                      Token 预算上限
                      <input
                        name="tokens"
                        type="number"
                        min="0"
                        defaultValue="500000"
                      />
                    </label>
                    <label>
                      子代理名额
                      <input
                        name="agents"
                        type="number"
                        min="0"
                        max="32"
                        defaultValue="0"
                      />
                    </label>
                    <label>
                      修复重试上限
                      <input
                        name="retries"
                        type="number"
                        min="0"
                        max="20"
                        defaultValue="2"
                      />
                    </label>
                  </div>
                </details>
                <button className="primary" disabled={busy}>
                  保存任务
                </button>
              </form>
            )}
            {modal.kind === "host-create" && (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  const data = new FormData(e.currentTarget);
                  action(async () => {
                    await api("/api/hosts", {
                      id: data.get("id"),
                      name: data.get("name"),
                      address: data.get("address"),
                      username: data.get("username") || null,
                      jump: data.get("jump") || null,
                      control_socket: data.get("socket"),
                      allowed_roots: String(data.get("roots") || "")
                        .split("\n")
                        .filter(Boolean),
                    });
                    setModal(null);
                  });
                }}
              >
                <div className="form-row">
                  <label>
                    主机标识
                    <input name="id" required placeholder="server-a2" />
                  </label>
                  <label>
                    显示名称
                    <input name="name" required placeholder="a2 工作机" />
                  </label>
                </div>
                <label>
                  SSH 地址或别名
                  <input name="address" required />
                </label>
                <div className="form-row">
                  <label>
                    用户名（可留空）
                    <input name="username" />
                  </label>
                  <label>
                    跳板路径（可留空）
                    <input name="jump" />
                  </label>
                </div>
                <label>
                  Codex 控制 socket 的完整路径
                  <input
                    name="socket"
                    placeholder="可留空，自动发现当前 SSH 账号的 Codex 端点"
                  />
                </label>
                <label>
                  允许的项目根目录（每行一个）
                  <textarea name="roots" rows={2} />
                </label>
                <button className="primary" disabled={busy}>
                  添加并连接
                </button>
              </form>
            )}
            {modal.kind === "report" && <pre className="report">{report}</pre>}
            {modal.kind === "proposal" && (
              <>
                <p>{modal.item?.reason}</p>
                <pre className="detail-json">
                  {JSON.stringify(modal.item?.patch, null, 2)}
                </pre>
                <div className="button-row">
                  <button
                    onClick={() =>
                      action(async () => {
                        await api(`/api/proposals/${modal.item?.id}/decide`, {
                          approved: false,
                        });
                        setModal(null);
                      })
                    }
                  >
                    拒绝
                  </button>
                  <button
                    className="primary"
                    onClick={() =>
                      action(async () => {
                        await api(`/api/proposals/${modal.item?.id}/decide`, {
                          approved: true,
                        });
                        setModal(null);
                      })
                    }
                  >
                    批准这次变更
                  </button>
                </div>
              </>
            )}
            {modal.kind === "task" && (
              <>
                <Badge state={modal.item?.status} />
                <h3>{modal.item?.title}</h3>
                <p className="detail-objective">{modal.item?.objective}</p>
                <dl>
                  <dt>工作目录</dt>
                  <dd>{modal.item?.cwd || "未分配"}</dd>
                  <dt>验收要求</dt>
                  <dd>{modal.item?.acceptance || "未设置"}</dd>
                  <dt>下一步</dt>
                  <dd>{modal.item?.next_step || "未记录"}</dd>
                  <dt>阻塞原因</dt>
                  <dd>{modal.item?.blocked_reason || "无"}</dd>
                  <dt>所属任务</dt>
                  <dd>
                    {state.tasks.find(
                      (task) => task.id === modal.item?.parent_id,
                    )?.title || "独立任务"}
                  </dd>
                  <dt>子任务</dt>
                  <dd>
                    {state.tasks
                      .filter((task) => task.parent_id === modal.item?.id)
                      .map((child) => (
                        <button
                          key={child.id}
                          onClick={() => open(child, "task")}
                        >
                          {child.title} · {label(child.status)}
                        </button>
                      ))}
                  </dd>
                  <dt>前置任务</dt>
                  <dd>
                    {(modal.item?.dependencies || [])
                      .map(
                        (id: string) =>
                          state.tasks.find((task) => task.id === id)?.title ||
                          id,
                      )
                      .join("、") || "无"}
                  </dd>
                  <dt>用量与名额</dt>
                  <dd>
                    {modal.item?.requests_used} /{" "}
                    {modal.item?.budget?.max_requests ?? "未设"} 请求 ·{" "}
                    {Number(modal.item?.tokens_used || 0).toLocaleString()} /{" "}
                    {modal.item?.budget?.max_tokens ?? "未设"} tokens · 子代理{" "}
                    {modal.item?.budget?.max_agents ?? 0}
                  </dd>
                </dl>
                {["planned", "ready", "waiting_resource"].includes(
                  modal.item?.status,
                ) && (
                  <button
                    className="primary"
                    disabled={busy || !modal.item?.host_id || !modal.item?.cwd}
                    onClick={() =>
                      action(async () => {
                        await api(`/api/tasks/${modal.item?.id}/dispatch`, {});
                      })
                    }
                  >
                    开始执行
                  </button>
                )}
                <p>
                  {state.sessions
                    .filter((session) => session.task_id === modal.item?.id)
                    .map((session) => (
                      <button
                        key={session.id}
                        onClick={() => open(session, "session")}
                      >
                        {session.name || session.thread_id.slice(0, 8)} ·{" "}
                        {label(session.status)}
                      </button>
                    ))}
                </p>
                <details>
                  <summary>调整预算</summary>
                  <form
                    key={modal.item?.id}
                    onSubmit={(e) => {
                      e.preventDefault();
                      const data = new FormData(e.currentTarget);
                      action(async () => {
                        await api(
                          `/api/tasks/${modal.item?.id}?expected=${modal.item?.revision}`,
                          {
                            budget: {
                              ...modal.item?.budget,
                              max_requests: Number(data.get("requests")),
                              max_tokens: Number(data.get("tokens")),
                              max_agents: Number(data.get("agents")),
                            },
                          },
                          "PATCH",
                        );
                      });
                    }}
                  >
                    <div className="form-row">
                      <label>
                        请求预算
                        <input
                          name="requests"
                          type="number"
                          min="0"
                          defaultValue={modal.item?.budget?.max_requests ?? 20}
                          required
                        />
                      </label>
                      <label>
                        Token 预算
                        <input
                          name="tokens"
                          type="number"
                          min="0"
                          defaultValue={
                            modal.item?.budget?.max_tokens ?? 500000
                          }
                          required
                        />
                      </label>
                      <label>
                        子代理名额
                        <input
                          name="agents"
                          type="number"
                          min="0"
                          max="32"
                          defaultValue={modal.item?.budget?.max_agents || 0}
                          required
                        />
                      </label>
                    </div>
                    <p className="subtle">
                      子代理名额用于新建会话；已有会话保持原生配置。所有请求继续受当前任务总预算约束。
                    </p>
                    <button disabled={busy}>保存预算</button>
                  </form>
                </details>
                <form
                  key={`verification-${modal.item?.id}`}
                  onSubmit={(e) => {
                    e.preventDefault();
                    const data = new FormData(e.currentTarget);
                    action(async () => {
                      await api(`/api/tasks/${modal.item?.id}/verify`, {
                        passed: data.get("passed") === "yes",
                        evidence: data.get("evidence"),
                      });
                      setModal(null);
                    });
                  }}
                >
                  <label>
                    验收证据
                    <textarea name="evidence" required />
                  </label>
                  <label>
                    验证结果
                    <select name="passed">
                      <option value="yes">通过，完成任务</option>
                      <option value="no">未通过，保留阻塞</option>
                    </select>
                  </label>
                  <button disabled={busy}>记录验证</button>
                </form>
              </>
            )}
            {modal.kind === "session" && (
              <>
                <p className="directory">{modal.item?.cwd}</p>
                <h3>当前目标</h3>
                <p className="detail-objective">
                  {modal.item?.goal?.objective ||
                    state.tasks.find((task) => task.id === modal.item?.task_id)
                      ?.objective ||
                    "尚未记录目标"}
                </p>
                {modal.item?.waiting_reason && (
                  <p>{modal.item.waiting_reason}</p>
                )}
                {modal.item?.last_error && (
                  <p className="error-text">{modal.item.last_error}</p>
                )}
                <div className="detail-meta">
                  <Badge state={modal.item?.status} />
                  <code>{modal.item?.model}</code>
                  <span>
                    goal 累计 {duration(modal.item?.goal?.timeUsedSeconds)}
                  </span>
                </div>
                <details>
                  <summary>关联工作任务与管理授权</summary>
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      const data = new FormData(e.currentTarget);
                      action(async () => {
                        await api(
                          `/api/sessions/${encodeURIComponent(modal.item!.id)}/bind`,
                          {
                            task_id: data.get("task_id"),
                            control_enabled: data.get("control") === "on",
                          },
                        );
                      });
                    }}
                  >
                    <label>
                      工作任务
                      <select
                        name="task_id"
                        defaultValue={modal.item?.task_id || ""}
                        required
                      >
                        <option value="" disabled>
                          选择已有任务
                        </option>
                        {state.tasks.map((task) => (
                          <option value={task.id} key={task.id}>
                            {task.title}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      <input
                        type="checkbox"
                        name="control"
                        defaultChecked={modal.item?.control_enabled}
                      />
                      允许在原任务和预算内自动管理
                    </label>
                    <button disabled={busy}>保存关联</button>
                  </form>
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      const data = new FormData(e.currentTarget);
                      action(async () => {
                        await api(
                          `/api/sessions/${encodeURIComponent(modal.item!.id)}/policy`,
                          {
                            enabled: data.get("enabled") === "on",
                            suspect_after: Number(data.get("suspect")),
                            confirm_after: Number(data.get("confirm")),
                            cooldown: Number(data.get("cooldown")),
                          },
                        );
                      });
                    }}
                  >
                    <label>
                      <input
                        type="checkbox"
                        name="enabled"
                        defaultChecked={modal.item?.stall_policy?.enabled}
                      />
                      启用长期停滞恢复
                    </label>
                    <div className="form-row">
                      <label>
                        诊断阈值（秒）
                        <input
                          name="suspect"
                          type="number"
                          min="1"
                          defaultValue={
                            modal.item?.stall_policy?.suspect_after || 900
                          }
                          required
                        />
                      </label>
                      <label>
                        确认阈值（秒）
                        <input
                          name="confirm"
                          type="number"
                          min="1"
                          defaultValue={
                            modal.item?.stall_policy?.confirm_after || 1800
                          }
                          required
                        />
                      </label>
                      <label>
                        恢复冷却（秒）
                        <input
                          name="cooldown"
                          type="number"
                          min="0"
                          defaultValue={
                            modal.item?.stall_policy?.cooldown ?? 1800
                          }
                          required
                        />
                      </label>
                    </div>
                    <button disabled={busy}>保存恢复设置</button>
                  </form>
                </details>
                <form
                  className="inline-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    const data = new FormData(e.currentTarget);
                    action(async () => {
                      await api(
                        `/api/sessions/${encodeURIComponent(modal.item!.id)}/command`,
                        {
                          action: "message",
                          payload: { text: data.get("text") },
                          operation_id: operationId(),
                        },
                      );
                    });
                  }}
                >
                  <input name="text" required placeholder="向此会话补充指令…" />
                  <button className="primary" disabled={busy}>
                    发送
                  </button>
                </form>
                <button
                  onClick={() =>
                    action(async () => {
                      await api(
                        `/api/sessions/${encodeURIComponent(modal.item!.id)}/command`,
                        {
                          action: "interrupt",
                          payload: { turn_id: modal.item?.active_turn_id },
                          operation_id: operationId(),
                        },
                      );
                    })
                  }
                  disabled={!modal.item?.active_turn_id || busy}
                >
                  中断当前回合
                </button>
                <div className="history-heading">
                  <h3>历史事件</h3>
                  <button
                    onClick={() => action(() => loadHistory(modal.item!.id))}
                  >
                    加载下一页
                  </button>
                </div>
                <div className="history">
                  {history.map((event) => (
                    <div key={event.seq}>
                      <small>
                        #{event.seq} · {clock(event.created)} ·{" "}
                        {event.body.method || event.kind}
                      </small>
                      <pre>
                        {output(event) || JSON.stringify(event.body, null, 2)}
                      </pre>
                    </div>
                  ))}
                  {!history.length && (
                    <p className="subtle">
                      按需加载历史，避免反复读取整个会话。
                    </p>
                  )}
                </div>
              </>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
function Terminal({ text, paused }: { text: string; paused: boolean }) {
  const ref = useRef<HTMLPreElement>(null);
  useEffect(() => {
    if (!paused && ref.current)
      ref.current.scrollTop = ref.current.scrollHeight;
  }, [text, paused]);
  return (
    <pre className="terminal" ref={ref}>
      {text || (
        <span className="terminal-empty">
          等待会话输出<span className="cursor">▍</span>
        </span>
      )}
    </pre>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
