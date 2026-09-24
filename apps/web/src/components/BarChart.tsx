/**
 * A minimal accessible horizontal bar chart.
 *
 * Hand-rolled instead of pulling in a charting library: the dashboard needs
 * horizontal bars and nothing else, and a chart the reader cannot inspect is
 * worse than a table. Every chart is paired with a real `<table>` in the
 * accessibility tree so the data is never image-only.
 *
 * The bars are HTML, not a scaled SVG: text stays at its native size in a
 * narrow card, the value column lines up, and long labels truncate with an
 * ellipsis (the full label is in the hover title and the table).
 */

export interface BarDatum {
  label: string;
  value: number;
  /** Text shown in the value column; defaults to the value. */
  display?: string;
  /**
   * `accent` for plain magnitude, `muted` for a secondary series drawn beside
   * it, and the status tones only where the bar reports a state.
   */
  tone?: "accent" | "muted" | "ok" | "warn" | "danger";
}

export function BarChart({
  title,
  data,
  max,
  unit = "",
}: {
  title: string;
  data: BarDatum[];
  max?: number;
  unit?: string;
}): JSX.Element {
  if (data.length === 0) {
    return <p className="muted">No data.</p>;
  }
  const ceiling = max ?? Math.max(...data.map((item) => item.value), 1);

  return (
    <figure className="bar-chart">
      <figcaption className="visually-hidden">{title}</figcaption>
      <div className="bar-chart-rows" role="img" aria-label={title}>
        {data.map((item) => {
          const shown = item.display ?? `${item.value}${unit}`;
          const share = ceiling > 0 ? Math.min(item.value / ceiling, 1) : 0;
          return (
            <div className="bar-row" key={item.label} title={`${item.label}: ${shown}`}>
              <span className="bar-label">{item.label}</span>
              <span className="bar-track">
                <span
                  className={`bar-fill bar-${item.tone ?? "accent"}`}
                  style={{ width: item.value > 0 ? `max(${share * 100}%, 3px)` : 0 }}
                />
              </span>
              <span className="bar-value">{shown}</span>
            </div>
          );
        })}
      </div>
      <table className="visually-hidden">
        <caption>{title}</caption>
        <thead>
          <tr>
            <th>Series</th>
            <th>Value</th>
          </tr>
        </thead>
        <tbody>
          {data.map((item) => (
            <tr key={item.label}>
              <td>{item.label}</td>
              <td>{item.display ?? `${item.value}${unit}`}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}
