import type { Job, JobItem } from "./api";
import { describeOptions } from "./Output";

const STAGES = [
  { key: "download", label: "Descarga" },
  { key: "processing", label: "Procesamiento" },
  { key: "upload", label: "Subida" },
] as const;

const STAGE_NAMES: Record<string, string> = {
  download: "la descarga",
  processing: "el procesamiento",
  upload: "la subida",
};

// En la vista previa, "complete" es lo hecho antes; en el resumen de una
// ejecución incluye lo que se acaba de subir.
const STATUS_LABELS: Record<JobItem["status"], string> = {
  complete: "En Drive",
  planned: "Se procesa",
  failed: "Con error",
  pending: "Pendiente de confirmar",
  not_started: "No iniciada",
};

const RELATED_LABELS: Record<string, string> = {
  previous_revision: "de una versión anterior",
  other_configuration: "de otra configuración",
  other_copy: "otra copia del mismo resultado",
};

// Con más imágenes sin problemas que estas, se agrupan para que los errores
// queden a la vista.
const GROUP_AFTER = 6;

const NEEDS_ATTENTION: JobItem["status"][] = ["failed", "pending", "not_started"];

const imageCount = (count: number) =>
  count === 1 ? "1 imagen" : `${count} imágenes`;

function ItemRow({ item, isPreview }: { item: JobItem; isPreview: boolean }) {
  const bad = item.status === "failed" || item.status === "pending";
  const label =
    isPreview && item.status === "complete" ? "Ya está en Drive" : STATUS_LABELS[item.status];

  return (
    <li className={bad ? "item bad" : "item"}>
      <div className="item-head">
        <span className="item-name">{item.name}</span>
        <span className={`item-status status-${item.status}`}>
          {label}
          {bad && item.stage ? ` en ${STAGE_NAMES[item.stage]}` : ""}
        </span>
      </div>
      {item.reason && <p className="item-detail">{item.reason}</p>}
      {item.notes.map((note) => (
        <p className="item-detail" key={note}>
          {note}
        </p>
      ))}
      {item.related.map((related) => (
        <p className="item-detail" key={related.name}>
          En Drive hay un PNG {RELATED_LABELS[related.kind] ?? "relacionado"}: {related.name}.
          No se modifica.
        </p>
      ))}
    </li>
  );
}

function Items({ items, isPreview }: { items: JobItem[]; isPreview: boolean }) {
  const attention = items.filter((item) => NEEDS_ATTENTION.includes(item.status));
  const rest = items.filter((item) => !NEEDS_ATTENTION.includes(item.status));
  const rows = (list: JobItem[]) =>
    list.map((item) => <ItemRow item={item} isPreview={isPreview} key={item.file_id} />);

  return (
    <>
      {attention.length > 0 && <ul className="items">{rows(attention)}</ul>}
      {rest.length > GROUP_AFTER ? (
        <details className="group">
          <summary>Ver las {imageCount(rest.length)} sin problemas</summary>
          <ul className="items">{rows(rest)}</ul>
        </details>
      ) : (
        rest.length > 0 && <ul className="items">{rows(rest)}</ul>
      )}
    </>
  );
}

function overallPercent(job: Job): number | null {
  const { total } = job.progress;
  if (!total) return null;
  const done = STAGES.reduce(
    (sum, { key }) => sum + Math.min(job.progress[key].done, total),
    0,
  );
  return Math.round((done / (STAGES.length * total)) * 100);
}

function Progress({ job }: { job: Job }) {
  const { progress } = job;
  const { total } = progress;
  const percent = overallPercent(job);
  const waitingForModel =
    job.kind === "run" &&
    total !== null &&
    total > 0 &&
    progress.download.done >= total &&
    progress.processing.done === 0;

  return (
    <>
      <div className="bar-row">
        <div
          className={percent === null ? "bar indeterminate" : "bar"}
          role="progressbar"
          aria-label="Avance total"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={percent ?? undefined}
        >
          <span style={{ width: percent === null ? undefined : `${percent}%` }} />
        </div>
        {percent !== null && <span className="bar-value">{percent} %</span>}
      </div>

      <ul className="progress" aria-label="Progreso">
        {STAGES.map(({ key, label }) => {
          const { done, failed } = progress[key];
          return (
            <li key={key} className={progress.stage === key ? "active" : undefined}>
              <span>{label}</span>
              <span>
                {total === null ? done : `${done} de ${total}`}
                {failed > 0 ? ` (${failed} con errores)` : ""}
              </span>
            </li>
          );
        })}
      </ul>
      {progress.current && <p className="hint">Última imagen: {progress.current}</p>}
      {waitingForModel && (
        <p className="hint">
          Preparando el modelo de IA. La primera vez se descarga y puede tardar varios minutos.
        </p>
      )}
      <p className="hint">
        Puede cerrar esta pestaña: el trabajo sigue mientras la aplicación esté abierta.
      </p>
    </>
  );
}

function Summary({ job, busy, onRun }: { job: Job; busy: boolean; onRun: () => void }) {
  const summary = job.summary!;
  const { counts } = summary;
  const toRun = counts.planned + counts.pending;
  const isPreview = job.kind === "preview";
  const unfinished = !isPreview && !summary.complete;
  const headline = isPreview
    ? "Así quedaría"
    : summary.complete
      ? "Listo: todo quedó en Drive"
      : "Terminó, pero hay imágenes para revisar";

  return (
    <>
      <p className={unfinished ? "headline warn" : "headline"}>{headline}</p>

      <ul className="counts">
        {isPreview ? (
          <>
            <li className="count accent">{counts.planned} por procesar</li>
            <li className="count">{counts.complete} ya en Drive</li>
          </>
        ) : (
          <>
            <li className="count accent">
              {summary.uploaded_now === 1
                ? "1 PNG subido ahora"
                : `${summary.uploaded_now} PNG subidos ahora`}
            </li>
            <li className="count">{counts.complete} en Drive</li>
          </>
        )}
        {counts.failed > 0 && <li className="count bad">{counts.failed} con errores</li>}
        {counts.pending > 0 && (
          <li className="count bad">
            {counts.pending === 1
              ? "1 pendiente de confirmar"
              : `${counts.pending} pendientes de confirmar`}
          </li>
        )}
        {counts.not_started > 0 && (
          <li className="count bad">
            {counts.not_started === 1 ? "1 no iniciada" : `${counts.not_started} no iniciadas`}
          </li>
        )}
      </ul>

      {summary.stopped && (
        <p className="alert" role="alert">
          Se detuvo en {STAGE_NAMES[summary.stopped.stage] ?? "una etapa"}: {summary.stopped.message}
        </p>
      )}

      {summary.items.length > 0 && <Items items={summary.items} isPreview={isPreview} />}

      {summary.warnings.map((warning) => (
        <p className="hint" key={warning}>
          {warning}
        </p>
      ))}

      {unfinished && (
        <p>Quedó trabajo sin terminar. Vuelva a generar la vista previa: lo ya hecho no se repite.</p>
      )}

      {isPreview && (
        <p className="hint">
          Los originales no se modifican ni se borran: se crean PNG nuevos en el destino. Es una
          estimación: lo que dependa de la descarga o del procesamiento se sabrá al ejecutar. Si
          agrega fotos a la carpeta antes de ejecutar, también se procesan.
        </p>
      )}

      {isPreview && !summary.can_run && !summary.stopped && (
        <p className="headline-small">No hay nada que procesar.</p>
      )}

      {summary.can_run && (
        <button className="primary large" onClick={onRun} disabled={busy}>
          Procesar {imageCount(toRun)}
        </button>
      )}
    </>
  );
}

export default function JobPanel({
  job,
  busy,
  onRun,
}: {
  job: Job;
  busy: boolean;
  onRun: () => void;
}) {
  const title = job.kind === "preview" ? "Vista previa" : "Procesamiento";

  return (
    <div className="job">
      <div className="job-head">
        <h3>{title}</h3>
        {job.options && <span className="tag">{describeOptions(job.options)}</span>}
      </div>

      {job.source.name && (
        <p className="job-folders">
          {job.same_folder
            ? `Carpeta: ${job.source.name}`
            : `De ${job.source.name} a ${job.target.name}`}
        </p>
      )}

      {job.status === "running" && <Progress job={job} />}
      {job.status === "finished" && job.summary && (
        <Summary job={job} busy={busy} onRun={onRun} />
      )}
      {job.error && (
        <p className="alert" role="alert">
          {job.error.message}
        </p>
      )}
    </div>
  );
}
