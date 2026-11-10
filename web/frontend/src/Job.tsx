import { useEffect, useRef, useState } from "react";
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
  removed: "Borrado de Drive",
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

// Qué hacer ante motivos conocidos del motor. El motor no se cambia: aquí
// solo se agrega la explicación para quien usa la pantalla.
const REASON_HINTS: { match: RegExp; hint: string }[] = [
  {
    match: /en la papelera/i,
    hint:
      "Si quiere conservarlo, restáurelo desde la papelera de Drive y genere otra vista " +
      "previa: figurará como ya en Drive.",
  },
];

/** Los motivos del motor llegan en minúscula: se muestran como oraciones. */
const sentence = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

const reasonHint = (reason: string) =>
  REASON_HINTS.find(({ match }) => match.test(reason))?.hint ?? null;

const imageCount = (count: number) =>
  count === 1 ? "1 imagen" : `${count} imágenes`;

const removedCount = (count: number) =>
  count === 1 ? "1 borrado de Drive" : `${count} borrados de Drive`;

/**
 * El tamaño de un nombre puesto como titular: uno largo baja de escala para
 * no ocupar la pantalla entera.
 */
export const nameLength = (name: string) =>
  name.length > 40 ? "long" : name.length > 20 ? "medium" : "short";

const comparable = (item: JobItem) => Boolean(item.images?.original && item.images?.result);

const prefersReducedMotion = () =>
  typeof window !== "undefined" &&
  typeof window.matchMedia === "function" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** El número sube contando hasta su nuevo valor. */
function useTweened(target: number, duration = 700): number {
  const [value, setValue] = useState(target);
  const from = useRef(target);

  useEffect(() => {
    const start = from.current;
    if (start === target || prefersReducedMotion() || typeof requestAnimationFrame !== "function") {
      from.current = target;
      setValue(target);
      return;
    }

    let frame = 0;
    const began = performance.now();
    const step = (now: number) => {
      const t = Math.min((now - began) / duration, 1);
      // Desaceleración exponencial: llega suave al valor final.
      const eased = t === 1 ? 1 : 1 - 2 ** (-10 * t);
      const current = Math.round(start + (target - start) * eased);
      from.current = current;
      setValue(current);
      if (t < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [target, duration]);

  return value;
}

/** Original y resultado superpuestos, con una línea que se arrastra. */
export function Compare({ job, item }: { job: Job; item: JobItem }) {
  const [position, setPosition] = useState(50);
  const version = job.finished_at ?? job.created_at;
  const base = `/api/jobs/current/images/${item.file_id}`;
  const ratio = job.options ? job.options.width / job.options.height : 1;
  const transparent = job.options?.background === "transparent";

  return (
    <figure className="compare" style={{ ["--pos" as string]: `${position}%` }}>
      <div className="compare-frame" style={{ ["--ratio" as string]: String(ratio) }}>
        <div className={transparent ? "compare-layer checker" : "compare-layer"}>
          <img src={`${base}/result?v=${version}`} alt={`Resultado de ${item.name}`} />
        </div>
        <div className="compare-layer compare-original">
          <img
            src={`${base}/original?aligned=1&v=${version}`}
            alt={`Original de ${item.name}`}
          />
        </div>
        <span className="compare-line" aria-hidden="true" />
        <span className="compare-knob" aria-hidden="true" />
        <span className="compare-tag compare-tag-left" aria-hidden="true">
          Original
        </span>
        <span className="compare-tag compare-tag-right" aria-hidden="true">
          Resultado
        </span>
        <input
          className="compare-range"
          type="range"
          min={0}
          max={100}
          value={position}
          aria-label={`Comparar original y resultado de ${item.name}`}
          onChange={(event) => setPosition(Number(event.target.value))}
        />
      </div>
      <figcaption className="item-name">{item.name}</figcaption>
    </figure>
  );
}

function ItemRow({
  item,
  isPreview,
  selected,
  onCompare,
}: {
  item: JobItem;
  isPreview: boolean;
  selected: boolean;
  onCompare: (item: JobItem) => void;
}) {
  const bad = item.status === "failed" || item.status === "pending";
  const label =
    isPreview && item.status === "complete" ? "Ya está en Drive" : STATUS_LABELS[item.status];
  const classes = ["item", bad ? "bad" : "", selected ? "selected" : ""].filter(Boolean);
  const hint = item.reason ? reasonHint(item.reason) : null;

  return (
    <li className={classes.join(" ")}>
      <div className="item-head">
        <span className="item-name">{item.name}</span>
        <span className="item-side">
          <span className={`item-status status-${item.status}`}>
            {label}
            {bad && item.stage ? ` en ${STAGE_NAMES[item.stage]}` : ""}
          </span>
          {comparable(item) && (
            <button className="small ghost" aria-pressed={selected} onClick={() => onCompare(item)}>
              Comparar
            </button>
          )}
        </span>
      </div>
      {item.status === "removed" && (
        <p className="item-detail">Se subió antes, pero ya no está en Drive.</p>
      )}
      {item.reason && <p className="item-detail">{sentence(item.reason)}</p>}
      {hint && <p className="item-detail item-hint">{hint}</p>}
      {item.notes.map((note) => (
        <p className="item-detail" key={note}>
          {sentence(note)}
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

function Items({
  items,
  isPreview,
  selectedId,
  onCompare,
}: {
  items: JobItem[];
  isPreview: boolean;
  selectedId: string | null;
  onCompare: (item: JobItem) => void;
}) {
  const attention = items.filter((item) => NEEDS_ATTENTION.includes(item.status));
  const rest = items.filter((item) => !NEEDS_ATTENTION.includes(item.status));
  const rows = (list: JobItem[]) =>
    list.map((item) => (
      <ItemRow
        item={item}
        isPreview={isPreview}
        selected={item.file_id === selectedId}
        onCompare={onCompare}
        key={item.file_id}
      />
    ));

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
  const shown = useTweened(percent ?? 0);
  const waitingForModel =
    job.kind === "run" &&
    total !== null &&
    total > 0 &&
    progress.download.done >= total &&
    progress.processing.done === 0;

  return (
    <div className="running">
      <div className="meter">
        {percent !== null && <span className="bar-value">{shown} %</span>}
        {percent === null && <span className="meter-label">Revisando las fotos…</span>}
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
      </div>

      <ul className="progress" aria-label="Progreso">
        {STAGES.map(({ key, label }) => {
          const { done, failed } = progress[key];
          const finished = total !== null && total > 0 && done >= total;
          const state = progress.stage === key ? "active" : finished ? "finished" : undefined;
          return (
            <li key={key} className={state}>
              <span>{label}</span>
              <span className="numbers">
                {total === null ? done : `${done} de ${total}`}
                {failed > 0 ? ` (${failed} con errores)` : ""}
              </span>
              <span className="track" aria-hidden="true">
                <span style={{ width: total ? `${Math.min(done / total, 1) * 100}%` : "0%" }} />
              </span>
            </li>
          );
        })}
      </ul>
      {progress.current && (
        <p className="hint current" key={progress.current}>
          Última imagen: {progress.current}
        </p>
      )}
      {waitingForModel && (
        <p className="hint">
          Preparando el modelo de IA. La primera vez se descarga y puede tardar varios minutos.
        </p>
      )}
      <p className="hint">
        Puede cerrar esta pestaña: el trabajo sigue mientras la aplicación esté abierta.
      </p>
    </div>
  );
}

/** La cifra principal del resumen, grande; el texto completo queda para lectores de pantalla. */
function Figure({ value, label }: { value: number; label: string }) {
  return (
    <li className="count figure">
      <span className="figure-number" aria-hidden="true">
        {value}
      </span>
      <span className="figure-label" aria-hidden="true">
        {label}
      </span>
      <span className="visually-hidden">
        {value} {label}
      </span>
    </li>
  );
}

/** Al final del resumen: los PNG que se borraron de Drive, y cómo recuperarlos. */
function RemovedNote({
  items,
  busy,
  onRegenerate,
}: {
  items: JobItem[];
  busy: boolean;
  onRegenerate: () => void;
}) {
  const one = items.length === 1;

  return (
    <section className="removed-note" aria-labelledby="removed-title">
      <h3 id="removed-title">PNG borrados de Drive</h3>
      <p>
        {one
          ? "Este PNG se subió antes, pero ya no está en Drive: "
          : "Estos PNG se subieron antes, pero ya no están en Drive: "}
        <strong>{items.map((item) => item.name).join(", ")}</strong>. Los PNG borrados de Drive
        no se vuelven a subir solos, por si los borró a propósito.
      </p>
      <div className="removed-actions">
        <button onClick={onRegenerate} disabled={busy}>
          {one ? "Volver a generarlo" : "Volver a generarlos"}
        </button>
        <p className="hint">Se prepara una vista previa nueva: nada se sube hasta que confirme.</p>
      </div>
    </section>
  );
}

function Summary({
  job,
  busy,
  onRun,
  onRegenerate,
}: {
  job: Job;
  busy: boolean;
  onRun: () => void;
  onRegenerate: () => void;
}) {
  const summary = job.summary!;
  const { counts } = summary;
  const toRun = counts.planned + counts.pending;
  const isPreview = job.kind === "preview";
  const unfinished = !isPreview && !summary.complete;
  const removed = summary.items.filter((item) => item.status === "removed");
  const headline = isPreview
    ? "Así quedaría"
    : !summary.complete
      ? "Terminó, pero hay imágenes para revisar"
      : removed.length > 0
        ? "Listo, con una nota"
        : "Listo: todo quedó en Drive";

  const first = summary.items.find(comparable) ?? null;
  const [selectedId, setSelectedId] = useState<string | null>(first?.file_id ?? null);
  const selected = summary.items.find((item) => item.file_id === selectedId && comparable(item));

  const tally = (
    <div className={unfinished ? "tally warn" : "tally"}>
      <p className="headline">{headline}</p>

      <ul className="counts">
        {isPreview ? (
          <>
            <Figure value={counts.planned} label="por procesar" />
            <li className="count">{counts.complete} ya en Drive</li>
          </>
        ) : (
          <>
            <Figure
              value={summary.uploaded_now}
              label={summary.uploaded_now === 1 ? "PNG subido ahora" : "PNG subidos ahora"}
            />
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
        {removed.length > 0 && <li className="count">{removedCount(removed.length)}</li>}
      </ul>

      {isPreview && !summary.can_run && !summary.stopped && (
        <p className="headline-small">No hay nada que procesar.</p>
      )}

      {summary.can_run && (
        <button className="primary large" onClick={onRun} disabled={busy}>
          Procesar {imageCount(toRun)}
        </button>
      )}
    </div>
  );

  return (
    <div
      className={selected ? "summary" : "summary no-view"}
      style={{ ["--ratio" as string]: String(job.options ? job.options.width / job.options.height : 1) }}
    >
      {selected && (
        <div className="summary-view">
          <Compare job={job} item={selected} key={selected.file_id} />
        </div>
      )}

      {tally}

      <div className="summary-list">
        {summary.stopped && (
          <p className="alert" role="alert">
            Se detuvo en {STAGE_NAMES[summary.stopped.stage] ?? "una etapa"}: {summary.stopped.message}
          </p>
        )}

        {summary.items.length > 0 && (
          <Items
            items={summary.items}
            isPreview={isPreview}
            selectedId={selected?.file_id ?? null}
            onCompare={(item) => setSelectedId(item.file_id)}
          />
        )}

        {summary.warnings.map((warning) => (
          <p className="hint" key={warning}>
            {warning}
          </p>
        ))}

        {removed.length > 0 && (
          <RemovedNote items={removed} busy={busy} onRegenerate={onRegenerate} />
        )}

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
      </div>
    </div>
  );
}

const jobTitle = (job: Job) => (job.kind === "preview" ? "Vista previa" : "Procesamiento");

const jobFolders = (job: Job) =>
  job.same_folder
    ? `Carpeta: ${job.source.name}`
    : `De ${job.source.name} a ${job.target.name}`;

function jobOutcome(job: Job): { text: string; warn: boolean } {
  if (job.status === "failed") return { text: "No se pudo completar.", warn: true };
  if (job.status === "interrupted")
    return { text: "Se interrumpió antes de terminar.", warn: true };

  const summary = job.summary;
  if (!summary) return { text: "", warn: false };

  const { counts } = summary;
  const errors = counts.failed + counts.pending + counts.not_started;
  const removed = counts.removed ? ` · ${removedCount(counts.removed)}` : "";

  if (job.kind === "preview") {
    const issues = errors > 0 ? ` · ${imageCount(errors)} con problemas` : "";
    return { text: `${counts.planned} por procesar${issues}${removed}`, warn: errors > 0 };
  }

  return summary.complete
    ? { text: `Listo: ${imageCount(counts.complete)} en Drive${removed}`, warn: false }
    : { text: `Terminó con ${imageCount(errors)} para revisar${removed}`, warn: true };
}

function jobDate(job: Job): string {
  const seconds = job.finished_at ?? job.created_at;
  try {
    return new Date(seconds * 1000).toLocaleString("es", {
      dateStyle: "medium",
      timeStyle: "short",
    });
  } catch {
    return "";
  }
}

/** De qué carpeta a cuál, en grande: lo primero que se lee del trabajo. */
function JobRoute({ job }: { job: Job }) {
  const source = job.source.name ?? "";
  const target = job.target.name ?? "";
  const longest = job.same_folder || source.length >= target.length ? source : target;
  const classes = `job-folders job-route ${nameLength(longest)}`;

  if (job.same_folder) {
    return (
      <p className={classes}>
        <span className="visually-hidden">Carpeta: </span>
        <span className="job-route-name">{job.source.name}</span>
      </p>
    );
  }

  return (
    <p className={classes}>
      <span className="visually-hidden">De </span>
      <span className="job-route-name">{job.source.name}</span>
      <span className="visually-hidden"> a </span>
      <span className="job-route-arrow" aria-hidden="true" />
      <span className="job-route-name">{job.target.name}</span>
    </p>
  );
}

/** El último trabajo, resumido en el inicio; se abre solo si se pide. */
export function LastJob({ job, onOpen }: { job: Job; onOpen: () => void }) {
  const outcome = jobOutcome(job);
  const when = jobDate(job);

  return (
    <section className="last-job" aria-labelledby="last-job-title">
      <div className="last-job-text">
        <h3 id="last-job-title">Último trabajo</h3>
        <p className="last-job-what">
          {jobTitle(job)}
          {when && <span className="last-job-when"> · {when}</span>}
        </p>
        {job.source.name && <p className="hint">{jobFolders(job)}</p>}
        {outcome.text && (
          <p className={outcome.warn ? "last-job-outcome warn" : "last-job-outcome"}>
            {outcome.text}
          </p>
        )}
      </div>
      <button onClick={onOpen}>Ver el último trabajo</button>
    </section>
  );
}

export default function JobPanel({
  job,
  busy,
  onRun,
  onClose,
  onRegenerate,
}: {
  job: Job;
  busy: boolean;
  onRun: () => void;
  onClose: () => void;
  onRegenerate: () => void;
}) {
  return (
    <div className="job">
      <header className="job-head">
        <div className="job-title">
          <h2>{jobTitle(job)}</h2>
          {job.status !== "running" && (
            <button className="small ghost" onClick={onClose}>
              Cerrar
            </button>
          )}
        </div>
        {job.source.name && <JobRoute job={job} />}
        {job.options && <span className="tag">{describeOptions(job.options)}</span>}
      </header>

      {job.status === "running" && <Progress job={job} />}
      {job.status === "finished" && job.summary && (
        <Summary job={job} busy={busy} onRun={onRun} onRegenerate={onRegenerate} key={job.id} />
      )}
      {job.error && (
        <p className="alert" role="alert">
          {job.error.message}
        </p>
      )}
    </div>
  );
}
