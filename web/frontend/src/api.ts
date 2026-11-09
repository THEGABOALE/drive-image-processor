// Cliente de la API local. El token CSRF es de esta ejecución del servidor.

import type { PickerConfig } from "./google/picker";

export type Account = {
  sub: string;
  email: string | null;
  display_name: string | null;
};

export type Connection = {
  state:
    | "disconnected"
    | "authorizing_read"
    | "authorizing_write"
    | "connected"
    | "reconnect_required";
  message: string | null;
  error: string | null;
  account: Account | null;
};

export type OutputLimits = {
  width: number;
  height: number;
  background: string;
  backgrounds: string[];
  min_side: number;
  max_side: number;
  max_pixels: number;
};

export type OutputOptions = { width: number; height: number; background: string };

export type Status = {
  environment: "development" | "production";
  csrf_token: string;
  connection: Connection;
  picker: PickerConfig;
  output?: OutputLimits;
};

export type Verification = {
  source: { id: string; name: string };
  target: { id: string; name: string };
  same_folder: boolean;
};

export type StageProgress = { done: number; failed: number };

export type JobItem = {
  file_id: string;
  name: string;
  // removed: un PNG subido antes que ya no está en Drive (se borró).
  status: "complete" | "planned" | "failed" | "pending" | "not_started" | "removed";
  stage: "download" | "processing" | "upload" | null;
  reason: string | null;
  actions: string[];
  undetermined: string[];
  notes: string[];
  related: { kind: string; name: string }[];
  // Si hay copias locales para comparar (falta en trabajos guardados antes).
  images?: { original: boolean; result: boolean };
};

export type Job = {
  id: string;
  kind: "preview" | "run";
  status: "running" | "finished" | "failed" | "interrupted";
  source: { id: string; name: string | null };
  target: { id: string; name: string | null };
  same_folder: boolean;
  created_at: number;
  finished_at: number | null;
  progress: {
    total: number | null;
    stage: string | null;
    current: string | null;
    download: StageProgress;
    processing: StageProgress;
    upload: StageProgress;
  };
  summary: {
    // removed falta en los trabajos guardados antes de existir.
    counts: Record<Exclude<JobItem["status"], "removed">, number> & { removed?: number };
    uploaded_now: number;
    complete: boolean;
    can_run: boolean;
    stopped: { stage: string; code: string; message: string } | null;
    warnings: string[];
    items: JobItem[];
  } | null;
  error: { code: string; message: string } | null;
  // Falta en los trabajos guardados antes de que existieran las opciones.
  options?: OutputOptions;
};

export class ApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const UNREACHABLE = new ApiError(
  "unreachable",
  "No se pudo comunicar con el servidor local de la aplicación.",
);

async function parse<T>(response: Response): Promise<T> {
  const body = await response.json();

  if (!response.ok) {
    throw new ApiError(
      body?.error?.code ?? "unexpected_error",
      body?.error?.message ?? "Error inesperado.",
    );
  }

  return body as T;
}

export async function getStatus(): Promise<Status> {
  try {
    return await parse<Status>(await fetch("/api/status"));
  } catch (error) {
    throw error instanceof ApiError ? error : UNREACHABLE;
  }
}

export async function getCurrentJob(): Promise<Job | null> {
  try {
    const body = await parse<{ job?: Job | null }>(await fetch("/api/jobs/current"));
    return body.job ?? null;
  } catch (error) {
    throw error instanceof ApiError ? error : UNREACHABLE;
  }
}

async function post<T>(path: string, csrfToken: string, body?: unknown): Promise<T> {
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return await parse<T>(response);
  } catch (error) {
    throw error instanceof ApiError ? error : UNREACHABLE;
  }
}

export const startConnection = (csrf: string) =>
  post<Connection>("/api/connection/start", csrf);

export const cancelConnection = (csrf: string) =>
  post<Connection>("/api/connection/cancel", csrf);

export const disconnect = (csrf: string) =>
  post<Connection>("/api/connection/disconnect", csrf);

// Solo los ID: el token del navegador nunca sale de google/picker.ts.
export const verifyFolders = (csrf: string, sourceId: string, targetId: string) =>
  post<Verification>("/api/folders/verify", csrf, {
    source_id: sourceId,
    target_id: targetId,
  });

export const startPreview = (
  csrf: string,
  sourceId: string,
  targetId: string,
  options: OutputOptions,
) =>
  post<Job>("/api/jobs/preview", csrf, {
    source_id: sourceId,
    target_id: targetId,
    options,
  });

// Las carpetas salen de la vista previa: esta solicitud no las envía.
export const startRun = (csrf: string, previewId: string) =>
  post<Job>(`/api/jobs/${previewId}/run`, csrf);

// Vuelve a generar los PNG del trabajo que se borraron de Drive: el backend
// lo confirma con Drive y responde con una vista previa nueva.
export const regenerateRemoved = (csrf: string, jobId: string) =>
  post<Job>(`/api/jobs/${jobId}/regenerate`, csrf);
