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

export type Status = {
  environment: "development" | "production";
  csrf_token: string;
  connection: Connection;
  picker: PickerConfig;
};

export type Verification = {
  source: { id: string; name: string };
  target: { id: string; name: string };
  same_folder: boolean;
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
