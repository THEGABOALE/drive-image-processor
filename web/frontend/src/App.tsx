import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  cancelConnection,
  disconnect,
  getCurrentJob,
  getStatus,
  startConnection,
  startPreview,
  startRun,
  verifyFolders,
  type Connection,
  type Job,
  type Status,
  type Verification,
} from "./api";
import {
  GooglePicker,
  loadGoogleApis,
  type PickedFolder,
  type PickerConfig,
} from "./google/picker";
import JobPanel from "./Job";
import "./styles.css";

type PickerLike = Pick<GooglePicker, "pick" | "forget">;

type AppProps = {
  createPicker?: (config: PickerConfig, expectedSub: string) => PickerLike;
  loadApis?: () => Promise<void>;
  pollMs?: number;
};

const AUTHORIZING_STEP: Partial<Record<Connection["state"], string>> = {
  authorizing_read: "paso 1 de 2: leer los originales",
  authorizing_write: "paso 2 de 2: crear los resultados",
};

const message = (error: unknown) =>
  error instanceof Error ? error.message : "Error inesperado.";

export default function App({
  createPicker = (config, sub) => new GooglePicker(config, sub),
  loadApis = loadGoogleApis,
  pollMs = 1500,
}: AppProps) {
  const [status, setStatus] = useState<Status | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [source, setSource] = useState<PickedFolder | null>(null);
  const [target, setTarget] = useState<PickedFolder | null>(null);
  const [sameFolder, setSameFolder] = useState(false);
  const [verification, setVerification] = useState<Verification | null>(null);
  const [busy, setBusy] = useState(false);
  const [pickerReady, setPickerReady] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const picker = useRef<PickerLike | null>(null);
  const apis = useRef<Promise<void> | null>(null);

  const connection = status?.connection;
  const csrf = status?.csrf_token ?? "";
  const pickerConfig = status?.picker;
  const connectedSub =
    connection?.state === "connected" ? (connection.account?.sub ?? "") : null;
  const authorizing = connection ? AUTHORIZING_STEP[connection.state] : undefined;
  const jobRunning = job?.status === "running";

  const refresh = useCallback(async () => {
    setStatus(await getStatus());
  }, []);

  useEffect(() => {
    refresh().catch((error) => setProblem(message(error)));
  }, [refresh]);

  useEffect(() => {
    if (!authorizing) return;
    const timer = setInterval(() => {
      refresh().catch(() => {});
    }, pollMs);
    return () => clearInterval(timer);
  }, [authorizing, refresh, pollMs]);

  // El trabajo sigue en el backend aunque se cierre o recargue la página.
  useEffect(() => {
    if (connectedSub === null) return;
    getCurrentJob()
      .then(setJob)
      .catch((error) => setProblem(message(error)));
  }, [connectedSub]);

  useEffect(() => {
    if (!jobRunning) return;
    const timer = setInterval(() => {
      Promise.all([getCurrentJob(), refresh()])
        .then(([current]) => setJob(current))
        .catch(() => {});
    }, pollMs);
    return () => clearInterval(timer);
  }, [jobRunning, refresh, pollMs]);

  // El Picker y lo elegido pertenecen a la cuenta conectada.
  useEffect(() => {
    if (connectedSub === null || !pickerConfig) return;

    const current = createPicker(pickerConfig, connectedSub);
    picker.current = current;
    apis.current ??= loadApis();
    apis.current
      .then(() => setPickerReady(true))
      .catch((error) => setProblem(message(error)));

    return () => {
      current.forget();
      picker.current = null;
      setSource(null);
      setTarget(null);
      setSameFolder(false);
      setVerification(null);
      setJob(null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectedSub, pickerConfig?.client_id]);

  const applyConnection = (next: Connection) =>
    setStatus((previous) => previous && { ...previous, connection: next });

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setProblem(null);
    try {
      await action();
    } catch (error) {
      setProblem(message(error));
    } finally {
      setBusy(false);
    }
  }

  const connect = () =>
    run(async () => applyConnection(await startConnection(csrf)));
  const cancel = () =>
    run(async () => applyConnection(await cancelConnection(csrf)));
  const leave = () => run(async () => applyConnection(await disconnect(csrf)));

  // Sin nada antes de pick(): el navegador solo permite la ventana de
  // autorización de Google como respuesta directa al clic.
  const choose = (title: string, assign: (folder: PickedFolder) => void) =>
    run(async () => {
      const folder = await picker.current?.pick(title);
      if (folder) {
        setVerification(null);
        setJob(null);
        assign(folder);
      }
    });

  const verify = () =>
    run(async () => {
      const targetId = sameFolder ? source?.id : target?.id;
      if (!source || !targetId) return;
      setVerification(null);
      try {
        setVerification(await verifyFolders(csrf, source.id, targetId));
      } catch (error) {
        if (
          error instanceof ApiError &&
          (error.code === "reconnect_required" || error.code === "not_connected")
        ) {
          await refresh();
        }
        throw error;
      }
    });

  const toggleSameFolder = (checked: boolean) => {
    setSameFolder(checked);
    setVerification(null);
    setJob(null);
  };

  const preview = () =>
    run(async () => {
      const targetId = sameFolder ? source?.id : target?.id;
      if (!source || !targetId) return;
      setJob(await startPreview(csrf, source.id, targetId));
    });

  const execute = () =>
    run(async () => {
      if (job) setJob(await startRun(csrf, job.id));
    });

  const ready = Boolean(source && (sameFolder || target));
  const connectionAlert = connection?.error ? connection.message : null;

  return (
    <div className="page">
      <header className="masthead">
        <h1>Drive Image Processor</h1>
        {status?.environment === "development" && (
          <span className="badge">Entorno de desarrollo</span>
        )}
      </header>

      <main>
        <section className="panel" aria-labelledby="account-title">
          <h2 id="account-title">Cuenta de Google Drive</h2>

          {!status && !problem && <p className="muted">Consultando el estado…</p>}

          {connection?.state === "connected" && (
            <div className="account">
              <div>
                <p className="account-name">
                  {connection.account?.email ?? "Cuenta conectada"}
                </p>
                {connection.account?.display_name && (
                  <p className="muted">{connection.account.display_name}</p>
                )}
              </div>
              <button className="quiet" onClick={leave} disabled={busy || jobRunning}>
                Desconectar
              </button>
            </div>
          )}

          {authorizing && (
            <div className="account">
              <p>Complete la autorización en el navegador ({authorizing}).</p>
              <button className="quiet" onClick={cancel} disabled={busy}>
                Cancelar
              </button>
            </div>
          )}

          {connection?.state === "disconnected" && (
            <>
              <p className="muted">
                Google pedirá dos autorizaciones: una para leer los originales y otra
                para crear los resultados. Use la misma cuenta en ambas.
              </p>
              <button onClick={connect} disabled={busy}>
                Conectar Google Drive
              </button>
            </>
          )}

          {connection?.state === "reconnect_required" && (
            <>
              <p>{connection.message ?? "La autorización ya no es válida."}</p>
              <button onClick={connect} disabled={busy}>
                Volver a conectar
              </button>
            </>
          )}
        </section>

        {connection?.state === "connected" && (
          <section className="panel" aria-labelledby="folders-title">
            <h2 id="folders-title">Carpetas</h2>

            <label className="check">
              <input
                type="checkbox"
                checked={sameFolder}
                disabled={jobRunning}
                onChange={(event) => toggleSameFolder(event.target.checked)}
              />
              Usar la misma carpeta como origen y destino
            </label>

            <div className="route">
              <FolderSlot
                label="Carpeta de origen"
                action="origen"
                folder={source}
                busy={busy || !pickerReady || jobRunning}
                onChoose={() => choose("Elegir la carpeta de origen", setSource)}
              />
              {!sameFolder && (
                <FolderSlot
                  label="Carpeta de destino"
                  action="destino"
                  folder={target}
                  busy={busy || !pickerReady || jobRunning}
                  onChoose={() => choose("Elegir la carpeta de destino", setTarget)}
                />
              )}
            </div>

            <div className="verify">
              <button onClick={verify} disabled={busy || !ready || jobRunning}>
                Verificar carpetas
              </button>
              {verification && (
                <p className="success" role="status">
                  {verification.same_folder
                    ? "Verificada: misma carpeta para originales y resultados."
                    : "Carpetas verificadas: se pueden leer los originales y crear los resultados."}
                </p>
              )}
              {verification && !jobRunning && (
                <button onClick={preview} disabled={busy}>
                  Ver vista previa
                </button>
              )}
            </div>
          </section>
        )}

        {connection?.state === "connected" && job && (
          <JobPanel job={job} busy={busy} onRun={execute} />
        )}

        {(problem || connectionAlert) && (
          <p className="alert" role="alert">
            {problem ?? connectionAlert}
          </p>
        )}
      </main>
    </div>
  );
}

function FolderSlot({
  label,
  action,
  folder,
  busy,
  onChoose,
}: {
  label: string;
  action: string;
  folder: PickedFolder | null;
  busy: boolean;
  onChoose: () => void;
}) {
  return (
    <div className="slot">
      <h3>{label}</h3>
      <p className={folder ? "folder-name" : "muted"}>
        {folder ? folder.name : "Sin elegir"}
      </p>
      <button className="quiet" onClick={onChoose} disabled={busy}>
        {folder ? `Cambiar ${action}` : `Elegir carpeta de ${action}`}
      </button>
    </div>
  );
}
