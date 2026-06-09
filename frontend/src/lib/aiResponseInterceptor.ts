import { invoke } from "@tauri-apps/api/core";

export interface BoardTaskInfo {
  id: string;
  title: string;
  description: string;
  status: string;
}

export interface DirectorActionStatus {
  status: "success" | "error";
  message: string;
  action: string;
  details?: unknown;
}

export interface ProcessAIResponseResult {
  visibleText: string;
  actions: DirectorActionStatus[];
  boardTasks: BoardTaskInfo[];
}

type RawActionBlock = {
  name?: unknown;
  arguments?: unknown;
};

type BoardMutateArguments = {
  action?: unknown;
  title?: unknown;
  description?: unknown;
  task_type?: unknown;
  status?: unknown;
  assigned_to?: unknown;
  assignedTo?: unknown;
  assigned_agent_id?: unknown;
  assignedAgentId?: unknown;
  priority?: unknown;
};

type StrictDirectorResponse = {
  chat_reply?: unknown;
  actions?: unknown;
};

const BOARD_ACTION_NAMES = new Set(["board_mutate"]);
const API_BASE = "http://127.0.0.1:8001";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const normalizeWhitespace = (text: string): string =>
  text
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();

const stripMarkdownFence = (text: string): string => {
  const trimmed = text.trim();
  const fenced = trimmed.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/i);
  return fenced ? fenced[1].trim() : trimmed;
};

const tryParseJson = (text: string): unknown | null => {
  try {
    return JSON.parse(stripMarkdownFence(text));
  } catch {
    return null;
  }
};

const parseStrictDirectorResponse = (rawText: string): StrictDirectorResponse | null => {
  const parsed = tryParseJson(rawText);
  if (!isRecord(parsed)) return null;
  if (!("chat_reply" in parsed) || !("actions" in parsed)) return null;
  if (!Array.isArray(parsed.actions)) return null;
  return parsed as StrictDirectorResponse;
};

const findMatchingJsonEnd = (text: string, start: number): number => {
  const opener = text[start];
  const closer = opener === "{" ? "}" : opener === "[" ? "]" : "";
  if (!closer) return -1;

  let depth = 0;
  let inString = false;
  let escaped = false;

  for (let i = start; i < text.length; i += 1) {
    const char = text[i];

    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (char === "\\") {
        escaped = true;
      } else if (char === '"') {
        inString = false;
      }
      continue;
    }

    if (char === '"') {
      inString = true;
      continue;
    }

    if (char === opener) depth += 1;
    if (char === closer) {
      depth -= 1;
      if (depth === 0) return i + 1;
    }
  }

  return -1;
};

const extractJsonCandidates = (rawText: string): Array<{ raw: string; start: number; end: number }> => {
  const candidates: Array<{ raw: string; start: number; end: number }> = [];
  const fencedRe = /```(?:json)?\s*([\s\S]*?)\s*```/gi;
  let match: RegExpExecArray | null;

  while ((match = fencedRe.exec(rawText)) !== null) {
    const block = match[1]?.trim();
    if (block) {
      candidates.push({ raw: block, start: match.index, end: match.index + match[0].length });
    }
  }

  const covered = (index: number) => candidates.some((candidate) => index >= candidate.start && index < candidate.end);

  for (let i = 0; i < rawText.length; i += 1) {
    if (covered(i)) continue;
    const char = rawText[i];
    if (char !== "{" && char !== "[") continue;

    const end = findMatchingJsonEnd(rawText, i);
    if (end === -1) continue;

    const raw = rawText.slice(i, end).trim();
    if (raw.includes("board_mutate") || raw.includes("action_type")) {
      candidates.push({ raw, start: i, end });
      i = end - 1;
    }
  }

  return candidates.sort((a, b) => a.start - b.start);
};

const parseActions = (parsed: unknown): RawActionBlock[] => {
  if (Array.isArray(parsed)) {
    return parsed.flatMap((item) => parseActions(item));
  }

  if (!isRecord(parsed)) return [];

  if (typeof parsed.name === "string") return [parsed as RawActionBlock];

  if (isRecord(parsed.function) && typeof parsed.function.name === "string") {
    const rawArgs = parsed.function.arguments;
    let args: unknown = rawArgs;
    if (typeof rawArgs === "string") {
      try {
        args = JSON.parse(rawArgs);
      } catch {
        args = {};
      }
    }
    return [{ name: parsed.function.name, arguments: args }];
  }

  if (Array.isArray(parsed.actions)) {
    return parsed.actions.flatMap((item) => parseActions(item));
  }

  if (parsed.action_type === "board_mutate") {
    const mutation = isRecord(parsed.mutation) ? parsed.mutation : parsed;
    return [{ name: "board_mutate", arguments: mutation }];
  }

  if (Array.isArray(parsed.tool_calls)) {
    return parsed.tool_calls.filter(isRecord) as RawActionBlock[];
  }

  return [];
};

const isExecutableActionBlock = (parsed: unknown): boolean =>
  parseActions(parsed).some((action) => typeof action.name === "string" && BOARD_ACTION_NAMES.has(action.name));

const taskStatus = (args: BoardMutateArguments): string => {
  const raw = args.status ?? args.task_type ?? "todo";
  return typeof raw === "string" && raw.trim() ? raw.trim() : "todo";
};

const assignedAgent = (args: BoardMutateArguments): string | undefined => {
  const raw = args.assignedAgentId ?? args.assigned_agent_id ?? args.assignedTo ?? args.assigned_to;
  return typeof raw === "string" && raw.trim() ? raw.trim() : undefined;
};

const addTaskToDb = async (args: BoardMutateArguments): Promise<BoardTaskInfo> => {
  const title = typeof args.title === "string" ? args.title.trim() : "";
  if (!title) throw new Error("board_mutate action has no title");

  const description = typeof args.description === "string" ? args.description.trim() : "";
  const assignedAgentId = assignedAgent(args);
  const status = taskStatus(args);

  try {
    return await invoke<BoardTaskInfo>("rust_add_task_to_db", {
      title,
      description,
      status,
      assignedAgentId: assignedAgentId || null,
    });
  } catch (primaryTauriError) {
    try {
      return await invoke<BoardTaskInfo>("add_task_to_db", {
        title,
        description,
        status,
        assignedAgentId: assignedAgentId || null,
      });
    } catch (tauriError) {
      const response = await fetch(`${API_BASE}/api/tasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title,
          description,
          assigned_agent_id: assignedAgentId || null,
        }),
      });

      if (!response.ok) {
        const detail = await response.text().catch(() => "");
        throw new Error(
          `rust_add_task_to_db failed: ${detail || `HTTP ${response.status}`}; primary_tauri=${String(
            primaryTauriError
          )}; tauri=${String(tauriError)}`
        );
      }

      const task = (await response.json()) as BoardTaskInfo;

      if (status !== "todo") {
        await fetch(`${API_BASE}/api/tasks/${task.id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status }),
        }).catch(() => undefined);
      }

      return task;
    }
  }
};

const sendDirectorStatus = async (status: DirectorActionStatus): Promise<void> => {
  try {
    await invoke("director_action_status", { status });
  } catch {
    console.info("Director hidden action status", status);
  }
};

const executeAction = async (action: RawActionBlock): Promise<{ status: DirectorActionStatus; task?: BoardTaskInfo }> => {
  const name = typeof action.name === "string" ? action.name : "";

  if (name !== "board_mutate") {
    return {
      status: {
        status: "error",
        action: name || "unknown",
        message: `Unsupported Director action: ${name || "unknown"}`,
      },
    };
  }

  if (!isRecord(action.arguments)) {
    return {
      status: {
        status: "error",
        action: name,
        message: "board_mutate arguments must be an object",
      },
    };
  }

  try {
    const args = action.arguments as BoardMutateArguments;
    const actionKind = typeof args.action === "string" ? args.action : "add_task";
    if (actionKind !== "add_task" && actionKind !== "create" && actionKind !== "add") {
      throw new Error(`Unsupported board_mutate arguments.action: ${actionKind}`);
    }
    const task = await addTaskToDb(args);
    return {
      task,
      status: {
        status: "success",
        action: name,
        message: "Task successfully added to Board",
        details: { taskId: task.id, title: task.title },
      },
    };
  } catch (error) {
    return {
      status: {
        status: "error",
        action: name,
        message: error instanceof Error ? error.message : String(error),
        details: action.arguments,
      },
    };
  }
};

const collectExecutableActionBlocks = (rawText: string) => {
  const candidates = extractJsonCandidates(rawText);
  const removableSpans: Array<{ start: number; end: number }> = [];
  const parsedActionBatches: RawActionBlock[][] = [];

  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(stripMarkdownFence(candidate.raw));
      if (!isExecutableActionBlock(parsed)) continue;

      const actions = parseActions(parsed).filter(
        (action) => typeof action.name === "string" && BOARD_ACTION_NAMES.has(action.name)
      );

      if (actions.length === 0) continue;
      removableSpans.push({ start: candidate.start, end: candidate.end });
      parsedActionBatches.push(actions);
    } catch (error) {
      console.warn("Failed to parse Director action JSON block", error, candidate.raw);
    }
  }

  return { removableSpans, parsedActionBatches };
};

export function stripAIActionBlocks(rawText: string): string {
  const strict = parseStrictDirectorResponse(rawText);
  if (strict) {
    return typeof strict.chat_reply === "string" ? normalizeWhitespace(strict.chat_reply) : "";
  }

  const { removableSpans } = collectExecutableActionBlocks(rawText);
  let visibleText = rawText;
  for (const span of removableSpans.sort((a, b) => b.start - a.start)) {
    visibleText = `${visibleText.slice(0, span.start)}${visibleText.slice(span.end)}`;
  }
  return normalizeWhitespace(visibleText);
}

export async function processAIResponse(rawText: string): Promise<ProcessAIResponseResult> {
  const strict = parseStrictDirectorResponse(rawText);
  const parsedActionBatches = strict
    ? [parseActions(strict).filter((action) => typeof action.name === "string" && BOARD_ACTION_NAMES.has(action.name))]
    : collectExecutableActionBlocks(rawText).parsedActionBatches;
  const visibleText = stripAIActionBlocks(rawText);

  const actions: DirectorActionStatus[] = [];
  const boardTasks: BoardTaskInfo[] = [];

  for (const batch of parsedActionBatches) {
    for (const action of batch) {
      const result = await executeAction(action);
      actions.push(result.status);
      if (result.task) boardTasks.push(result.task);
      await sendDirectorStatus(result.status);

      if (result.status.status === "error") {
        console.error("Director action failed", result.status);
      }
    }
  }

  return {
    visibleText: visibleText || (boardTasks.length ? "Задача добавлена в Board." : ""),
    actions,
    boardTasks,
  };
}
