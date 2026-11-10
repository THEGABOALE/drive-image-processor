import { useRef, useState } from "react";
import type { OutputLimits, OutputOptions } from "./api";

export type OutputDraft = { width: string; height: string; background: string };

export const BACKGROUND_LABELS: Record<string, string> = {
  white: "Blanco",
  transparent: "Transparente",
};

export const draftFrom = (limits: OutputLimits): OutputDraft => ({
  width: String(limits.width),
  height: String(limits.height),
  background: limits.background,
});

type Side = "width" | "height";

const OTHER: Record<Side, Side> = { width: "height", height: "width" };

const parseSide = (value: string): number | null => {
  const number = Number(value);
  return value.trim() !== "" && Number.isFinite(number) ? Math.round(number) : null;
};

/**
 * El mayor valor que admite un lado, con los mismos límites que valida el
 * motor: el máximo por lado y el total de píxeles junto con el otro lado.
 */
function sideMax(draft: OutputDraft, side: Side, limits: OutputLimits): number {
  const other = parseSide(draft[OTHER[side]]);
  const otherSide = Math.max(other ?? limits.min_side, limits.min_side);
  return Math.min(limits.max_side, Math.floor(limits.max_pixels / otherSide));
}

const clamp = (value: number, min: number, max: number) => Math.min(Math.max(value, min), max);

/** Un lado dentro de los límites; si no es un número, vuelve a `fallback`. */
function fitSide(
  draft: OutputDraft,
  side: Side,
  limits: OutputLimits,
  fallback: string,
): OutputDraft {
  const value = parseSide(draft[side]) ?? parseSide(fallback) ?? limits[side];
  return { ...draft, [side]: String(clamp(value, limits.min_side, sideMax(draft, side, limits))) };
}

/** El tamaño listo para el motor: los dos lados dentro de los límites. */
export function fitDraft(draft: OutputDraft, limits: OutputLimits): OutputDraft {
  const width = fitSide(draft, "width", limits, String(limits.width));
  return fitSide(width, "height", limits, String(limits.height));
}

export const optionsFrom = (draft: OutputDraft): OutputOptions => ({
  width: Number(draft.width),
  height: Number(draft.height),
  background: draft.background,
});

export function describeOptions(options: OutputOptions): string {
  const background = (BACKGROUND_LABELS[options.background] ?? options.background).toLowerCase();
  return `${options.width} × ${options.height} px, fondo ${background}`;
}

// Arrastrar: cada 2 píxeles de mouse suman 10; con Mayúsculas, 1 por píxel.
const DRAG_THRESHOLD = 3;

function SizeField({
  label,
  side,
  draft,
  limits,
  disabled,
  onChange,
}: {
  label: string;
  side: Side;
  draft: OutputDraft;
  limits: OutputLimits;
  disabled: boolean;
  onChange: (draft: OutputDraft) => void;
}) {
  // El valor al entrar al campo: si se deja vacío o inválido, se vuelve a él.
  const before = useRef(draft[side]);
  const [dragging, setDragging] = useState(false);

  const change = (value: string) => {
    const next = { ...draft, [side]: value };
    const number = parseSide(value);
    // Por arriba se corrige al instante: seguir escribiendo solo agranda el
    // número. Por abajo se espera a salir del campo (1 puede ser el inicio de 120).
    if (number !== null && number > sideMax(next, side, limits)) {
      onChange(fitSide(next, side, limits, before.current));
    } else {
      onChange(next);
    }
  };

  const fit = () => {
    const fitted = fitSide(draft, side, limits, before.current);
    if (fitted[side] !== draft[side]) onChange(fitted);
  };

  return (
    <label className="field">
      {label}
      <input
        type="number"
        inputMode="numeric"
        className={dragging ? "scrub dragging" : "scrub"}
        title="Arrastre a los lados para cambiarlo, o haga clic para escribir"
        min={limits.min_side}
        max={sideMax(draft, side, limits)}
        step={1}
        value={draft[side]}
        disabled={disabled}
        onFocus={() => {
          before.current = draft[side];
        }}
        onBlur={fit}
        onKeyDown={(event) => {
          if (event.key === "Enter") fit();
        }}
        onChange={(event) => change(event.target.value)}
        onPointerDown={(event) => {
          if (disabled || event.button !== 0) return;
          const input = event.currentTarget;
          const startX = event.clientX;
          const start = Number(fitSide(draft, side, limits, before.current)[side]);
          let moved = false;

          const move = (moveEvent: PointerEvent) => {
            const dx = moveEvent.clientX - startX;
            if (!moved && Math.abs(dx) < DRAG_THRESHOLD) return;
            if (!moved) {
              moved = true;
              input.setPointerCapture?.(moveEvent.pointerId);
              input.blur();
              setDragging(true);
            }
            const delta = moveEvent.shiftKey ? dx : Math.round(dx / 2) * 10;
            onChange(
              fitSide({ ...draft, [side]: String(start + delta) }, side, limits, String(start)),
            );
          };
          // En la ventana, no en el campo: un movimiento rápido sale del campo
          // antes de que el arrastre empiece.
          const stop = () => {
            setDragging(false);
            window.removeEventListener("pointermove", move);
            window.removeEventListener("pointerup", stop);
            window.removeEventListener("pointercancel", stop);
          };
          window.addEventListener("pointermove", move);
          window.addEventListener("pointerup", stop);
          window.addEventListener("pointercancel", stop);
        }}
      />
    </label>
  );
}

export default function OutputForm({
  draft,
  limits,
  disabled,
  onChange,
}: {
  draft: OutputDraft;
  limits: OutputLimits;
  disabled: boolean;
  onChange: (draft: OutputDraft) => void;
}) {
  return (
    <div className="output">
      <div className="output-controls">
        <div className="sizes">
          <SizeField
            label="Ancho (px)"
            side="width"
            draft={draft}
            limits={limits}
            disabled={disabled}
            onChange={onChange}
          />
          <span className="times" aria-hidden="true">
            ×
          </span>
          <SizeField
            label="Alto (px)"
            side="height"
            draft={draft}
            limits={limits}
            disabled={disabled}
            onChange={onChange}
          />
        </div>

        <fieldset className="backgrounds" disabled={disabled}>
          <legend>Fondo</legend>
          {limits.backgrounds.map((background) => (
            <label className="swatch-option" key={background}>
              <input
                type="radio"
                name="background"
                value={background}
                checked={draft.background === background}
                disabled={disabled}
                onChange={() => onChange({ ...draft, background })}
              />
              <span className={`swatch canvas-${background}`} aria-hidden="true" />
              {BACKGROUND_LABELS[background] ?? background}
            </label>
          ))}
        </fieldset>

      </div>

      <p className="hint">
        Si cambia el tamaño o el fondo, se crean resultados nuevos con otro nombre; los anteriores
        no se modifican.
      </p>
    </div>
  );
}
